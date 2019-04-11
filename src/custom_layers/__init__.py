import tensorflow as tf

from .spectral_normalization import ConvSN2D, DenseSN
from .batch_normalization import SyncBatchNorm


def UnPooling2D():
  def call(x):
    x = tf.transpose(a=x, perm=[1, 2, 3, 0])
    x = tf.expand_dims(x, 0)
    x = tf.tile(x, [4, 1, 1, 1, 1])
    x = tf.batch_to_space(x, [2, 2], [[0, 0], [0, 0]])
    x = tf.transpose(a=x[0], perm=[3, 0, 1, 2])
    return x
  def output_shape(input_shape):
    n, h, w, c = input_shape
    return (n, h * 2, w * 2, c)
  return tf.keras.layers.Lambda(call, output_shape=output_shape)

def DropChannels(output_dim):
  def call(x):
    return x[..., :output_dim]
  def output_shape(input_shape):
    return input_shape[:-1] + (output_dim,)
  return tf.keras.layers.Lambda(call, output_shape=output_shape)

