# Copyright 2019 The DDSP Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Lint as: python3
"""Library of neural network functions."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import ddsp.core
import gin
import tensorflow.compat.v1 as tf

tfkl = tf.keras.layers


# ------------------ Normalization ---------------------------------------------
def normalize_op(x, norm_type='layer', eps=1e-5):
  """Apply either Group, Instance, or Layer normalization."""
  mb, h, w, ch = x.shape
  n_groups = {'instance': ch, 'layer': 1, 'group': 32}[norm_type]

  x = tf.reshape(x, [mb, h, w, n_groups, ch // n_groups])
  mean, var = tf.nn.moments(x, [1, 2, 4], keepdims=True)
  x = (x - mean) / tf.sqrt(var + eps)
  x = tf.reshape(x, [mb, h, w, ch])
  return x


class Normalize(tfkl.Layer):
  """Normalization layer with learnable parameters."""

  def __init__(self, norm_type='layer'):
    super(Normalize, self).__init__()
    self.norm_type = norm_type

  def build(self, x_shape):
    self.scale = self.add_weight(
        name='scale',
        shape=[1, 1, 1, int(x_shape[-1])],
        dtype=tf.float32,
        initializer=tf.ones_initializer)
    self.shift = self.add_weight(
        name='shift',
        shape=[1, 1, 1, int(x_shape[-1])],
        dtype=tf.float32,
        initializer=tf.zeros_initializer)

  def call(self, x):
    x = normalize_op(x, self.norm_type)
    return (x * self.scale) + self.shift


# ------------------ ResNet ----------------------------------------------------
def norm_relu_conv(ch, k, s, norm_type, name='norm_relu_conv'):
  """Downsample frequency by stride."""
  layers = [
      Normalize(norm_type),
      tfkl.Activation(tf.nn.relu),
      tfkl.Conv2D(ch, (k, k), (1, s), padding='same', name='conv2d'),
  ]
  return tf.keras.Sequential(layers, name=name)


class ResidualLayer(tfkl.Layer):
  """A single layer for ResNet, with a bottleneck."""

  def __init__(self, ch, stride, shortcut, norm_type, name='residual_layer'):
    """Downsample frequency by stride, upsample channels by 4."""
    super(ResidualLayer, self).__init__(name=name)
    ch_out = 4 * ch
    self.shortcut = shortcut

    # Layers.
    self.norm_input = Normalize(norm_type)
    if self.shortcut:
      self.conv_proj = tfkl.Conv2D(ch_out, (1, 1), (1, stride), padding='same',
                                   name='conv_proj')
    layers = [
        tfkl.Conv2D(ch, (1, 1), (1, 1), padding='same', name='conv2d'),
        norm_relu_conv(ch, 3, stride, norm_type),
        norm_relu_conv(ch_out, 1, 1, norm_type),
    ]
    self.bottleneck = tf.keras.Sequential(layers, name='bottleneck')

  def call(self, x):
    r = x
    x = tf.nn.relu(self.norm_input(x))
    # The projection shortcut should come after the first norm and ReLU
    # since it performs a 1x1 convolution.
    r = self.conv_proj(x) if self.shortcut else r
    x = self.bottleneck(x)
    return x + r


def residual_stack(filters,
                   block_sizes,
                   strides,
                   norm_type,
                   name='residual_stack'):
  """ResNet layers."""
  layers = []
  for (ch, n_layers, stride) in zip(filters, block_sizes, strides):
    # Only the first block per residual_stack uses shortcut and strides.
    layers.append(ResidualLayer(ch, stride, True, norm_type))
    # Add the additional (n_layers - 1) layers to the stack.
    for _ in range(1, n_layers):
      layers.append(ResidualLayer(ch, 1, False, norm_type))
  layers.append(Normalize(norm_type))
  layers.append(tfkl.Activation(tf.nn.relu))
  return tf.keras.Sequential(layers, name=name)


@gin.register
def resnet(size='large', norm_type='layer', name='resnet'):
  """Residual network."""
  size_dict = {
      'small': (32, [2, 3, 4]),
      'medium': (32, [3, 4, 6]),
      'large': (64, [3, 4, 6]),
  }
  ch, blocks = size_dict[size]
  layers = [
      tfkl.Conv2D(64, (7, 7), (1, 2), padding='same', name='conv2d'),
      tfkl.MaxPool2D(pool_size=(1, 3), strides=(1, 2), padding='same'),
      residual_stack([ch, 2 * ch, 4 * ch], blocks, [1, 2, 2], norm_type),
      residual_stack([8 * ch], [3], [2], norm_type)
  ]
  return tf.keras.Sequential(layers, name=name)


# ------------------ Stacks ----------------------------------------------------
def dense(ch, name='dense'):
  return tfkl.Dense(ch, name=name)


def fc(ch=256, name='fc'):
  layers = [
      dense(ch),
      tfkl.LayerNormalization(),
      tfkl.Activation(tf.nn.leaky_relu),
  ]
  return tf.keras.Sequential(layers, name=name)


def fc_stack(ch=256, layers=2, name='fc_stack'):
  return tf.keras.Sequential([fc(ch) for _ in range(layers)], name=name)


def rnn(dims, rnn_type, return_sequences=True):
  rnn_class = {'lstm': tfkl.LSTM, 'gru': tfkl.GRU}[rnn_type]
  return rnn_class(dims, return_sequences=return_sequences, name=rnn_type)


# ------------------ Utilities -------------------------------------------------
@gin.register
def split_to_dict(tensor, tensor_splits):
  """Split a tensor into a dictionary of multiple tensors."""
  labels = [v[0] for v in tensor_splits]
  sizes = [v[1] for v in tensor_splits]
  tensors = tf.split(tensor, sizes, axis=-1)
  return dict(zip(labels, tensors))


# ------------------ Fade-in/out during training -------------------------------
@gin.register
def linear_fade(iter_start, iter_end, min_value=0.0, max_value=1.0):
  """Clipped linear fade for scaling in quantities during training.

  Args:
    iter_start: Iteration to start the fade (value=min_value before).
    iter_end: Iteration to end the fade (value=max_value after).
    min_value: Value to start the fade at.
    max_value: Value to end the fade at.

  Returns:
    A float32 linearly scaled between 0 and 1 based upon training iteration.
  """
  iter_start = tf.cast(iter_start, tf.float32)
  iter_end = tf.cast(iter_end, tf.float32)
  min_value = tf.cast(min_value, tf.float32)
  max_value = tf.cast(max_value, tf.float32)
  global_step = tf.cast(tf.train.get_or_create_global_step(), tf.float32)
  slope = 1.0 / (iter_end - iter_start)
  intercept = -1.0 * slope * iter_start
  fade = tf.clip_by_value(global_step * slope + intercept, 0.0, 1.0)
  value = min_value + (max_value - min_value) * fade
  return value


@gin.register
def exp_fade(iter_start, iter_end, start_value=1e-3):
  """Exponential fade between min_value and 1.0. during training."""
  fade = linear_fade(iter_start, iter_end, -5.0, 5.0)
  return ddsp.core.exp_sigmoid(
      fade, exponent=10.0, max_value=1.0, threshold=start_value)


