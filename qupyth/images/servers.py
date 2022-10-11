from abc import ABC, abstractmethod
from multiprocessing.sharedctypes import Value
from typing import Union, List, Tuple
from dataclasses import dataclass
from enum import Enum
from PIL import Image, ImageCms

import io
import warnings
import numpy as np



@dataclass(frozen=True)
class Region2D:
    """
    Simple data class to represent the bounding box for a region in 2D.
    This optionally includes a downsample value to indicate that the 
    region may be required at a lower resolution.
    """
    downsample: float = None
    x: int = 0
    y: int = 0
    width: int = -1
    height: int = -1
    z: int = 0
    t: int = 0

    def scale_region(self, scale_factor: float = None) -> "Region2D":
        if scale_factor is None:
            return self.downsample_region()
        return self.downsample_region(1.0/scale_factor)


    def downsample_region(self, downsample: float = None) -> "Region2D":
        """
        Downsample the bounding box for the Region2D.
        This can be used to convert coordinates from (for example) the full image resolution 
        to a different pyramidal level.
        """
        if downsample is None:
            downsample = self.downsample

        if downsample is None or downsample == 1:
            return self

        if downsample == 0:
            raise ValueError('Downsample cannot be 0!')
        
        x = int(self.x / downsample)
        y = int(self.y / downsample)
        # Handle -1 for width & height, i.e. until the full image width
        if self.width == -1:
            x2 = x-1
        else:
            x2 = int(round(self.x + self.width) / downsample)
        if self.height == -1:
            y2 = y-1
        else:
            y2 = int(round(self.y + self.height) / downsample)
        return Region2D(downsample=None, x=x, y=y, width=x2-x, height=y2-y, z=self.z, t=self.t)      



class Units(Enum):
    """
    Simple enum for length units.
    Currently only 'pixels' and 'µm' are supported.
    """
    PIXELS = 'px'
    MICRONS = 'μm'


@dataclass
class PixelCalibration:
    """
    Simple data class for storing pixel calibration information.
    Currently only the width, height and depth (z-spacing) are supported 
    and units are assumed to be the same in all dimensions.
    """
    pixel_width: float = 1.0
    pixel_height: float = 1.0
    pixel_depth: float = 1.0
    units: Units = Units.PIXELS


@dataclass
class ImageServerMetadata:
    """
    Simple data class to store core metadata for a pyramidal image.
    """
    path: str
    name: str
    shape: Tuple[int]  # hwczt format
    downsamples: Tuple[float]
    pixel_calibration: PixelCalibration
    is_rgb: bool
    dtype: np.dtype



class ImageServer(ABC):

    def __init__(self):
        super().__init__()
        self._metadata = None

    def _level_for_downsample(self, downsample: float):
        downsamples = self.downsamples
        if downsample <= downsamples[0]:
            return 0
        elif downsample >= downsamples[-1]:
            return len(downsamples) - 1
        else:
            # Allow a little bit of a tolerance because OpenSlide calculates downsamples
            # using the ratio of image dimensions... and can end up a little bit off
            for level, d in reversed(list(enumerate(downsamples))):
                if downsample >= d - 1e-3:
                    return level
        return 0


    def read_region(self,
                    region: Union[Region2D, Tuple[int, ...]] = None,
                    downsample: float = None,
                    x: int = 0,
                    y: int = 0,
                    width: int = -1,
                    height: int = -1,
                    z: int = 0,
                    t: int = 0
                    ) -> np.ndarray:
        """
        Read pixels from any arbitrary image region, at any resolution.
        Coordinates are provided in the coordinate space of the full-resolution image.

        This means that any downsample will applied, impacting the width/height of the returned image.
        This may result in off-by-one issues due to user-expectation and rounding; these can be avoided by using
        :func:`read_block` if the downsample corresponds exactly to an existing level.

        :param region:
        :param downsample:
        :return:
        """

        if region is None:
            region = Region2D(downsample=downsample,
                              x=0, y=0, width=width, height=height, z=z, t=t)
        elif isinstance(region, Tuple):
            # If we have a tuple, use it along with the downsample if available
            if downsample is None:
                region = Region2D(*region)
            else:
                region = Region2D((downsample,) + region)
        
        if ~isinstance(region, Region2D):
            raise ValueError('No valid region provided to read_region method')

        all_downsamples = self.downsamples
        level = _get_level(all_downsamples, region.downsample)
        level_downsample = all_downsamples[level]

        block = region.downsample_region(downsample=level_downsample)
        im = self.read_block(level=level, block=block)
        if downsample == level_downsample:
            return im
        target_size = (round(region.width / region.downsample), round(region.height / region.downsample))
        return _resize(im, target_size, resample=self.resize_method)

    @property
    def resize_method(self) -> Image.Resampling:
        """
        Resampling method to use when resizing the image for downsampling.
        Subclasses can override this, e.g. to enforce nearest neighbor resampling for label images.
        """
        return Image.Resampling.BICUBIC

    @abstractmethod
    def _build_metadata(self) -> ImageServerMetadata:
        ...

    @property
    def is_rgb(self) -> bool:
        return self.metadata.is_rgb

    @abstractmethod
    def read_block(self, level: int, block: Tuple[int, ...]) -> np.ndarray:
        """
        Read a block of pixels from a specific level.
        Coordinates are provided in the coordinate space of the level, NOT the full-resolution image.
        This means that the returned image should have the width and height specified.
        :param level:
        :param block:
        :return:
        """
        pass

    def _dim_length(self, dim: int):
        shape = self.metadata.shape
        return shape[dim] if len(shape) > dim else 1

    @property
    def metadata(self) -> ImageServerMetadata:
        if self._metadata is None:
            self._metadata = self._build_metadata()
        return self._metadata

    @property
    def shape(self) -> Tuple:
        return self.metadata.shape

    @property
    def dtype(self) -> np.dtype:
        return self.metadata.dtype

    @property
    def width(self) -> int:
        return self._dim_length(1)

    @property
    def height(self) -> int:
        return self._dim_length(0)

    @property
    def n_channels(self) -> int:
        return self._dim_length(2)

    @property
    def n_resolutions(self) -> int:
        return len(self.metadata.downsamples)

    @property
    def n_timepoints(self) -> int:
        return self._dim_length(4)

    @property
    def n_z_slices(self) -> int:
        return self._dim_length(3)

    @property
    def downsamples(self) -> Tuple[float]:
        return self.metadata.downsamples

    @property
    def path(self) -> str:
        return self.metadata.path

    @property
    def name(self) -> str:
        return self.metadata.name

    def close(self):
        pass



class WrappedImageServer(ImageServer):
    """
    Abstract class for an ImageServer that wraps another ImageServer, 
    e.g. to transform the image in some way.
    """

    def __init__(self, base_server: ImageServer):
        super().__init__()
        self._base_server = base_server

    @property
    def base_server(self) -> ImageServer:
        return self._base_server

    def _build_metadata(self) -> ImageServerMetadata:
        return self._base_server.metadata

    @property
    def path(self) -> str:
        return self._base_server.path + ' (wrapped)'


class IccProfileServer(WrappedImageServer):
    """
    Wrap an ImageServer and apply an ICC Profile to the pixels, if possible.

    If no ICC Profile is provided, an attempt is made to read the profile from the image using PIL.
    This isn't guaranteed to succeed.
    To find out if it was successful, test whether self.icc_transform is not None.

    See http://www.andrewjanowczyk.com/application-of-icc-profiles-to-digital-pathology-images/ 
    for a blog post describing where this may be useful, and providing further code.
    """

    def __init__(self, base_server: ImageServer, icc_profile: Union[bytes, ImageCms.ImageCmsProfile, ImageCms.ImageCmsTransform]=None):
        super().__init__(base_server)

        try:
            if isinstance(icc_profile, ImageCms.ImageCmsProfile) or isinstance(icc_profile, bytes):
                srgb = ImageCms.createProfile("sRGB")
                icc_profile = ImageCms.buildTransformFromOpenProfiles(srgb, icc_profile, "RGB", "RGB")

            if isinstance(icc_profile, ImageCms.ImageCmsTransform):
                self._icc = icc_profile
            else:
                self._icc = _read_icc(base_server.metadata.path)
        except:
            warnings.warn(f'No ICC Profile found for {base_server.path}') 
            self._icc = None

    @property
    def icc_transform(self) -> ImageCms.ImageCmsTransform:
        """
        Get the transform used to apply the ICC profile.
        If this is None, then the server simply returns the original pixels unchanged.
        """
        return self._icc
    
    def read_block(self, *args, **kwargs) -> np.ndarray:
        im = self.base_server.read_block(*args, **kwargs)
        if self._icc:
            image = Image.fromarray(im)
            image = ImageCms.applyTransform(image, self._icc)
            im = np.array(image)
        return im

    @property
    def path(self) -> str:
        return self._base_server.path + ' (+ICC Profile)'
    

def _get_icc_bytes(path) -> bytes:
    # Temporarily remove max pixel limit used to avoid decompression bomb DOS attach error 
    # since there's a good chance we have a very large (whole slide) image
    max_pixels = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = None
    try:
        icc = Image.open(path).info.get('icc_profile', None)
        if None:
            warnings.warn(f'No ICC Profile found')
        return io.BytesIO(icc)
    except Exception as e:
        warnings.warn(f'Unable to read ICC Profile: {e}')
        return None
    finally:
        Image.MAX_IMAGE_PIXELS = max_pixels


def _read_icc(path) -> ImageCms.ImageCmsTransform:
    icc_bytes = _get_icc_bytes(path)
    if icc_bytes is None:
        return None
    srgb = ImageCms.createProfile("sRGB")
    return ImageCms.buildTransformFromOpenProfiles(srgb, icc_bytes, "RGB", "RGB")




def _compute_length(length: float, downsample: float = None) -> int:
    """
    Helper function for computing an image list (width or height) with downsampling,
    using a consistent approach to rounding.
    :param length:
    :param downsample:
    :return:
    """
    return int(round(length / downsample))



def _validate_block(block: Union[Region2D, Tuple[int, ...]]) -> Region2D:
    """
    Convert a variable length tuple into a Region2D, corresponding to the 2D bounding box
    for an image region in the form (x, y, width, height, z, t).
    Note that the tuple should be in the order (x, y, width, height, z, t) and default values 
    will be used for any that are missing.

    The downsample value for the Region2D will be None.
    """
    if isinstance(block, Region2D):
        return block

    return Region2D(*((None,) + block))    
    
    # if len(block) == 6:
    #     return block
    # if len(block) < 4:
    #     raise ValueError(f'Block required as least 4 entries (x, y, width, height): receieved {block}')
    # elif len(block) < 6:
    #     return block + ('',) * (6 - len(block))


def _get_size(im: Union[np.ndarray, Image.Image]):
    """
    Get the size of an image as a two-element tuple (width, height).
    """
    return im.size if isinstance(im, Image.Image) else im.shape[:2][::-1]


def _resize(im: Union[np.ndarray, Image.Image], target_size: Tuple[int, int], resample: int = Image.Resampling.BICUBIC):
    """
    Resize an image to a target size.

    This uses the implementation from PIL; watch out for image types that may not be supported.

    :param im:
    :param target_size:
    :param resample:
    :return:
    """
    if _get_size(im) == target_size:
        return im
    do_convert = isinstance(im, np.ndarray)
    if do_convert:
        n_channels = 1 if im.ndim == 2 else im.shape[2]
        if n_channels == 1 and im.shape[2] == 1:
            im = im.squeeze()
        if im.dtype == np.uint8:
            modes8 = {3: 'RGB', 4: 'RGBA'}
            mode = modes8.get(n_channels, 'P')
        else:
            mode = None
        # TODO: Check if channel counts other than 1, 3, 4 supported
        image = Image.fromarray(im, mode=mode)
    else:
        image = im
    image = image.resize(size=target_size, resample=resample)
    return np.asarray(image) if do_convert else image



def _get_level(all_downsamples: Tuple[float], downsample: float, abs_tol=1e-3) -> int:
    """
    Get the level (index) from a list of downsamples that is best for fulfilling an image region request.

    This is the index of the entry in all_downsamples that either (almost) matches the requested downsample,
    or relates to the next highest resolution image (so that any required scaling is to reduce resolution).

    :param all_downsamples:  all available downsamples
    :param downsample:       requested downsample value
    :param abs_tol:          absolute tolerance when comparing downsample values; this allows for a stored downsample
                             value to be slightly off due to rounding
                             (e.g. requesting 4.0 would match a level 4 +/- abs_tol)
    :return:
    """
    downsamples = all_downsamples
    if len(downsamples) == 1 or downsample <= downsamples[0]:
        return 0
    elif downsample >= downsamples[-1]:
        return len(downsamples) - 1
    else:
        # Allow a little bit of a tolerance because downsamples are often calculated
        # by rounding the ratio of image dimensions... and can end up a little bit off
        for level, d in reversed(list(enumerate(downsamples))):
            if downsample >= d - abs_tol:
                return level
        return 0