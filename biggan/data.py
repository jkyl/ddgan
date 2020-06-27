import tensorflow as tf
import numpy as np
import threading
import tempfile
import logging
import glob
import copy
import sys
import cv2
import os

from tensorflow.python.client import device_lib

from numpy.lib.format import _write_array_header
from numpy.lib.format import dtype_to_descr
from numpy.lib.format import open_memmap
from numpy.lib.format import write_array
from numpy.lib.npyio import zipfile_factory
from numpy.compat import os_fspath

from contextlib import contextmanager
from contextlib import closing

from joblib import Parallel
from joblib import delayed


def get_gpus():
    """
    Returns the identities of all available GPUs
    """
    return [d.name for d in device_lib.list_local_devices() if d.device_type == "GPU"]


def get_strategy():
    """
    Returns a mirrored strategy over all available GPUs,
    or falls back to CPU if no GPUs available
    """
    return tf.distribute.MirroredStrategy(devices=(get_gpus() or ["/CPU:0"]))


def preprocess(img):
    """
    Casts a tensor's type to half-precision float,
    then scales its values to the range [-1, 1]
    """
    return tf.cast(img, tf.float32) / 127.5 - 1


def postprocess(img):
    """
    Scales a tensor's values to the range [0, 255],
    then casts its type to unsigned 8-bit integer
    """
    return tf.cast(tf.clip_by_value(img * 127.5 + 127.5, 0, 255), tf.uint8)


def get_per_replica_batch_size(global_batch_size):
    """
    Given the desired global batch size, returns the per-replica
    batch size based on the number of compute devices available.
    """
    n_gpus = len(get_gpus())
    if not n_gpus:
        pass
    elif global_batch_size % n_gpus:
        raise ValueError(
            "batch size ({}) is not evenly divisible by number of GPUs ({})".format(
                global_batch_size, n_gpus
            )
        )
    else:
        global_batch_size //= n_gpus
    return global_batch_size


def get_train_data(data_file, batch_size, n_threads=4, cache=True):
    """
    Creates a training data pipeline that samples batches
    from an array of pre-cropped image data in the provided
    .npy file, caching it in memory unless otherwise provided
    """

    archive = np.load(data_file, mmap_mode=None if cache else "r")
    features, labels = archive["features"], archive["labels"]
    n, h, w, c = features.shape

    def random_sample_generator():
        while True:
            batch_inds = np.random.randint(n, size=batch_size)
            yield features[batch_inds], labels[batch_inds]

    ds = tf.data.Dataset.from_generator(
        random_sample_generator,
        (tf.uint8, tf.int32),
        ((batch_size, h, w, c), (batch_size,)),
    )
    ds = ds.map(lambda img, cls: (preprocess(img), cls), n_threads)
    ds = ds.prefetch(n_threads)

    # Set the number of classes in the dataset as an attribute.
    ds.num_classes = labels.max() + 1
    return ds


def glob_image_files(data_dir):
    """
    Searches the given directory's subdirectories for
    JPEG and PNG files, and returns a list of their filenames
    """
    groups = [
        group
        for group in [
            sorted(glob.glob(os.path.join(subdir, "*." + extension)))
            for subdir in sorted(glob.glob(os.path.join(data_dir, "*/")))
            for extension in ("jpg", "jpeg", "png")
            for extension in (extension.upper(), extension.lower())
        ]
        if group
    ]
    files = [filename for group in groups for filename in group]
    labels = [i for i, group in enumerate(groups) for _ in group]
    return files, labels


class ImageTooSmall(Exception):
    """
    Exception for images that are too small.
    """


def load_crop_resize_img(filename, image_size):
    """
    Loads an image from disk, crops into a square along
    its major axis, then downsamples to the given size
    """

    # load the image and get its size
    image = cv2.imread(filename)
    size = image.shape[:2]
    min_ax = np.argmin(size)

    # make sure the image is at least as big as the final size
    if size[min_ax] < image_size:
        raise ImageTooSmall(size)

    # construct a crop
    crop = ()
    for j, dim in enumerate(size):
        if j == min_ax:
            start = stop = None
        else:
            start = (size[j] - size[min_ax]) // 2
            stop = start + size[min_ax]
        crop += (slice(start, stop, None),)
    crop += (slice(None, None, -1),)

    # apply the crop and resize
    return cv2.resize(
        image[crop], (image_size, image_size), interpolation=cv2.INTER_AREA
    )


def resize_npy_file(memmap, new_shape):
    """
    Rewrite the header and truncate a .npy file to the desired shape
    """
    memmap.flush()
    dtype = np.dtype(memmap.dtype)
    with open(os_fspath(memmap.filename), "rb+") as fp:
        _write_array_header(
            fp, dict(descr=dtype_to_descr(dtype), fortran_order=False, shape=new_shape,)
        )
        offset = fp.tell()
    output = np.memmap(
        memmap.filename,
        dtype=dtype,
        shape=new_shape,
        order="C",
        mode="r+",
        offset=offset,
    )
    output.flush()
    return output


@contextmanager
def temporary_file():
    os_handle, filename = tempfile.mkstemp()
    try:
        yield filename
    finally:
        os.remove(filename)


def create_dataset(data_dir, output_npz, image_size=256):
    """
    Loads PNG and JPEG image files within possibly nested
    directories, crops them, downsamples them to the target size,
    puts them all into a single numpy array, then writes to disk.
    """
    assert sys.version_info >= (3, 6) # simplifies writing to zipfiles

    # get the image filenames
    files, classes = glob_image_files(data_dir)
    num_files = len(files)
    if num_files == 0:
        raise IOError('no image files found in "{}"'.format(data_dir))

    # manage deletion of temporary files
    with temporary_file() as features_file, temporary_file() as labels_file:

        # memory-map arrays to store all the images
        features = open_memmap(
            filename=features_file,
            shape=(num_files, image_size, image_size, 3),
            dtype=np.uint8,
            mode="w+",
        )
        labels = open_memmap(
            filename=labels_file, shape=(num_files,), dtype=np.int32, mode="w+",
        )
        # this variable counts the number of reserved indices in the output
        mutable_target = [0]

        # within the lock context, access by threads is exclusive
        lock = threading.Lock()

        @delayed
        def process(index, target=mutable_target):
            """
            Loads, processes, and stores an image in the memmap
            defined in the outer scope
            """
            try:
                img = load_crop_resize_img(files[index], image_size)
            except ImageTooSmall as err:
                logging.warning(
                    "{}: {} on file {}; skipping...".format(
                        type(err).__name__, err, files[index]
                    )
                )
            else:
                with lock:  # reserve the output index
                    reservation = copy.copy(target[0])
                    target[0] += 1
                features[reservation] = img
                labels[reservation] = classes[index]

        # call the thread executor on the `process` function
        Parallel(prefer="threads")(map(process, range(num_files)))

        # rewrite the headers and truncate the files to exclude unused space
        features = resize_npy_file(features, tuple(mutable_target) + features.shape[1:])
        labels = resize_npy_file(labels, tuple(mutable_target))

        # write the images and labels to a .npz file
        with closing(zipfile_factory(output_npz, mode="w")) as zipf:
            for key, val in [("features.npy", features), ("labels.npy", labels)]:
                with zipf.open(key, "w", force_zip64=True) as fid:
                    write_array(fid, val)