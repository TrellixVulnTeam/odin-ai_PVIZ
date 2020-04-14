import os

import numpy as np
import tensorflow as tf

from odin.utils.net_utils import download_and_extract


# ===========================================================================
# Helpers
# ===========================================================================
def _partition(part, train=None, valid=None, test=None, unlabeled=None):
  r""" A function for automatically select the right data partition """
  part = str(part).lower().strip()
  if 'train' in part:
    ret = train
  elif 'valid' in part:
    ret = valid
  elif 'test' in part:
    ret = test
  elif 'unlabeled' in part:
    ret = unlabeled
  else:
    raise ValueError("No support for partition with name: '%s'" % part)
  if ret is None:
    raise ValueError("No data for parition with name: '%s'" % part)
  return ret


class ImageDataset:

  def sample_images(self, save_path=None, dpi=80, n_samples=25, seed=1):
    n = int(np.sqrt(n_samples))
    assert n * n == n_samples, "Sqrt of n_samples is not an integer"
    train = self.create_dataset(batch_size=n_samples,
                                partition='train',
                                inc_labels=False)
    images = [x for x in train.take(10)]
    rand = np.random.RandomState(seed=seed)
    images = images[rand.choice(10)].numpy()
    # plot and save the figure
    if save_path is not None:
      plot_images = images
      if plot_images.shape[-1] == 1:
        plot_images = np.squeeze(plot_images, axis=-1)
      from matplotlib import pyplot as plt
      fig = plt.figure(figsize=(16, 16))
      for i in range(n_samples):
        plt.subplot(n, n, i + 1)
        img = plot_images[i]
        plt.imshow(img)
        plt.axis('off')
      plt.tight_layout()
      fig.savefig(save_path, dpi=int(dpi))
      plt.close(fig)
    return images

  def normalize_255(self, image):
    return tf.clip_by_value(image / 255., 1e-6, 1. - 1e-6)

  @property
  def n_labels(self):
    return len(self.labels)

  @property
  def labels(self):
    return np.array([])

  @property
  def shape(self):
    raise NotImplementedError()

  @property
  def is_binary(self):
    raise NotImplementedError()

  def create_dataset(self,
                     batch_size=64,
                     drop_remainder=False,
                     shuffle=1000,
                     prefetch=tf.data.experimental.AUTOTUNE,
                     cache='',
                     parallel=None,
                     partition='train',
                     inc_labels=False,
                     **kwargs) -> tf.data.Dataset:
    raise NotImplementedError()


# ===========================================================================
# Dataset
# ===========================================================================
class BinarizedMNIST(ImageDataset):
  r""" BinarizedMNIST """

  def __init__(self):
    import tensorflow_datasets as tfds
    self.train, self.valid, self.test = tfds.load(
        name='binarized_mnist',
        split=['train', 'validation', 'test'],
        as_supervised=False)

  @property
  def is_binary(self):
    return True

  @property
  def shape(self):
    return (28, 28, 1)

  def create_dataset(self,
                     batch_size=64,
                     drop_remainder=False,
                     shuffle=1000,
                     prefetch=tf.data.experimental.AUTOTUNE,
                     cache='',
                     parallel=None,
                     partition='train',
                     inc_labels=False) -> tf.data.Dataset:
    r"""
    Arguments:
      partition : {'train', 'valid', 'test'}
      inc_labels : a Boolean. If True, return both image and label, otherwise,
        only image is returned.

    Return :
      train, test, unlabeled : `tensorflow.data.Dataset`
        image - `(tf.float32, (28, 28, 1))`
        label - `(tf.float32, (10,))`
    """
    ds = _partition(partition,
                    train=self.train,
                    valid=self.valid,
                    test=self.test)
    struct = tf.data.experimental.get_structure(ds)
    if len(struct) == 1:
      inc_labels = False
    ids = tf.range(self.n_labels, dtype=tf.float32)

    def _process_dict(data):
      image = tf.cast(data['image'], tf.float32)
      if not self.is_binary:
        image = self.normalize_255(image)
      if inc_labels:
        label = tf.cast(data['label'], tf.float32)
        return image, label
      return image

    def _process_tuple(*data):
      image = tf.cast(data[0], tf.float32)
      if not self.is_binary:
        image = self.normalize_255(image)
      if inc_labels:
        label = tf.cast(data[1], tf.float32)
        if len(label.shape) == 0:
          label = tf.cast(ids == label, tf.float32)
        return image, label
      return image

    ds = ds.map(_process_dict if isinstance(struct, dict) else _process_tuple,
                parallel)
    if cache is not None:
      ds = ds.cache(str(cache))
    # shuffle must be called after cache
    if shuffle is not None:
      ds = ds.shuffle(int(shuffle))
    ds = ds.batch(batch_size, drop_remainder)
    if prefetch is not None:
      ds = ds.prefetch(prefetch)
    return ds


class MNIST(BinarizedMNIST):
  r""" MNIST """

  def __init__(self):
    import tensorflow_datasets as tfds
    self.train, self.valid, self.test = tfds.load(
        name='mnist',
        split=['train[:90%]', 'train[90%:]', 'test'],
        shuffle_files=True,
        as_supervised=True)

  @property
  def labels(self):
    return np.array([str(i) for i in range(10)])

  @property
  def is_binary(self):
    return False

  @property
  def shape(self):
    return (28, 28, 1)


class BinarizedAlphaDigits(BinarizedMNIST):
  r""" Binary 20x16 digits of '0' through '9' and capital 'A' through 'Z'.
  39 examples of each class. """

  def __init__(self):
    import tensorflow_datasets as tfds
    self.train, self.valid, self.test = tfds.load(
        name='binary_alpha_digits',
        split=['train[:70%]', 'train[70%:80%]', 'train[80%:]'],
        as_supervised=True,
        shuffle_files=True,
    )

  @property
  def shape(self):
    return (20, 16, 1)
