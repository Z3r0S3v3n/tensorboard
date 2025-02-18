# Copyright 2018 The TensorFlow Authors. All Rights Reserved.
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
# ==============================================================================
"""Histogram summaries and TensorFlow operations to create them, V2 versions.

A histogram summary stores a list of buckets. Each bucket is encoded as
a triple `[left_edge, right_edge, count]`. Thus, a full histogram is
encoded as a tensor of dimension `[k, 3]`.

In general, the value of `k` (the number of buckets) will be a constant,
like 30. There are two edge cases: if there is no data, then there are
no buckets (the shape is `[0, 3]`); and if there is data but all points
have the same value, then there is one bucket whose left and right
endpoints are the same (the shape is `[1, 3]`).
"""

import contextlib

import numpy as np

from tensorboard.compat import tf2 as tf
from tensorboard.compat.proto import summary_pb2
from tensorboard.plugins.histogram import metadata
from tensorboard.util import lazy_tensor_creator
from tensorboard.util import tensor_util


DEFAULT_BUCKET_COUNT = 30


def histogram(name, data, step=None, buckets=None, description=None):
    """Write a histogram summary.

    See also `tf.summary.scalar`, `tf.summary.SummaryWriter`.

    Writes a histogram to the current default summary writer, for later analysis
    in TensorBoard's 'Histograms' and 'Distributions' dashboards (data written
    using this API will appear in both places). Like `tf.summary.scalar` points,
    each histogram is associated with a `step` and a `name`. All the histograms
    with the same `name` constitute a time series of histograms.

    The histogram is calculated over all the elements of the given `Tensor`
    without regard to its shape or rank.

    This example writes 2 histograms:

    ```python
    w = tf.summary.create_file_writer('test/logs')
    with w.as_default():
        tf.summary.histogram("activations", tf.random.uniform([100, 50]), step=0)
        tf.summary.histogram("initial_weights", tf.random.normal([1000]), step=0)
    ```

    A common use case is to examine the changing activation patterns (or lack
    thereof) at specific layers in a neural network, over time.

    ```python
    w = tf.summary.create_file_writer('test/logs')
    with w.as_default():
    for step in range(100):
        # Generate fake "activations".
        activations = [
            tf.random.normal([1000], mean=step, stddev=1),
            tf.random.normal([1000], mean=step, stddev=10),
            tf.random.normal([1000], mean=step, stddev=100),
        ]

        tf.summary.histogram("layer1/activate", activations[0], step=step)
        tf.summary.histogram("layer2/activate", activations[1], step=step)
        tf.summary.histogram("layer3/activate", activations[2], step=step)
    ```

    Arguments:
      name: A name for this summary. The summary tag used for TensorBoard will
        be this name prefixed by any active name scopes.
      data: A `Tensor` of any shape. The histogram is computed over its elements,
        which must be castable to `float64`.
      step: Explicit `int64`-castable monotonic step value for this summary. If
        omitted, this defaults to `tf.summary.experimental.get_step()`, which must
        not be None.
      buckets: Optional positive `int`. The output will have this
        many buckets, except in two edge cases. If there is no data, then
        there are no buckets. If there is data but all points have the
        same value, then there is one bucket whose left and right
        endpoints are the same.
      description: Optional long-form description for this summary, as a
        constant `str`. Markdown is supported. Defaults to empty.

    Returns:
      True on success, or false if no summary was emitted because no default
      summary writer was available.

    Raises:
      ValueError: if a default writer exists, but no step was provided and
        `tf.summary.experimental.get_step()` is None.
    """
    # Avoid building unused gradient graphs for conds below. This works around
    # an error building second-order gradient graphs when XlaDynamicUpdateSlice
    # is used, and will generally speed up graph building slightly.
    data = tf.stop_gradient(data)
    summary_metadata = metadata.create_summary_metadata(
        display_name=None, description=description
    )
    # TODO(https://github.com/tensorflow/tensorboard/issues/2109): remove fallback
    summary_scope = (
        getattr(tf.summary.experimental, "summary_scope", None)
        or tf.summary.summary_scope
    )

    # Try to capture current name scope so we can re-enter it below within our
    # histogram_summary helper. We do this to avoid having the `tf.cond` below
    # insert an extra `cond` into the tag name.
    # TODO(https://github.com/tensorflow/tensorboard/issues/2885): Remove this
    # special handling once the format no longer requires dynamic output shapes.
    name_scope_cms = []
    if hasattr(tf, "get_current_name_scope"):
        # Coerce None to ""; this API should only return a string but as of TF
        # 2.5 it returns None in contexts that re-enter the empty scope.
        current_scope = tf.get_current_name_scope() or ""
        # Append a "/" to the scope name, which causes that scope to be treated
        # as absolute instead of relative to the current scope, so that we can
        # re-enter it. It also prevents auto-incrementing of the scope name.
        # This is legacy graph mode behavior, undocumented except in comments:
        # https://github.com/tensorflow/tensorflow/blob/v2.5.0/tensorflow/python/framework/ops.py#L6664-L6666
        scope_to_reenter = current_scope + "/" if current_scope else ""
        name_scope_cms.append(tf.name_scope(scope_to_reenter))

    def histogram_summary(data, buckets, histogram_metadata, step):
        with contextlib.ExitStack() as stack:
            for cm in name_scope_cms:
                stack.enter_context(cm)
            with summary_scope(
                name, "histogram_summary", values=[data, buckets, step]
            ) as (tag, _):
                # Defer histogram bucketing logic by passing it as a callable to
                # write(), wrapped in a LazyTensorCreator for backwards
                # compatibility, so that we only do this work when summaries are
                # actually written.
                @lazy_tensor_creator.LazyTensorCreator
                def lazy_tensor():
                    return _buckets(data, buckets)

                return tf.summary.write(
                    tag=tag,
                    tensor=lazy_tensor,
                    step=step,
                    metadata=summary_metadata,
                )

    # `_buckets()` has dynamic output shapes which is not supported on TPU's.
    # To address this, explicitly mark this logic for outside compilation so it
    # will be executed on the CPU, and skip it entirely if we aren't actually
    # recording summaries to avoid overhead of transferring data.
    # TODO(https://github.com/tensorflow/tensorboard/issues/2885): Remove this
    # special handling once the format no longer requires dynamic output shapes.
    if isinstance(
        tf.distribute.get_strategy(),
        (tf.distribute.experimental.TPUStrategy, tf.distribute.TPUStrategy),
    ):
        return tf.cond(
            tf.summary.should_record_summaries(),
            lambda: tf.compat.v1.tpu.outside_compilation(
                histogram_summary, data, buckets, summary_metadata, step
            ),
            lambda: False,
        )
    return histogram_summary(data, buckets, summary_metadata, step)


def _buckets(data, bucket_count=None):
    """Create a TensorFlow op to group data into histogram buckets.

    Arguments:
      data: A `Tensor` of any shape. Must be castable to `float64`.
      bucket_count: Optional positive `int` or scalar `int32` `Tensor`.
    Returns:
      A `Tensor` of shape `[k, 3]` and type `float64`. The `i`th row is
      a triple `[left_edge, right_edge, count]` for a single bucket.
      The value of `k` is either `bucket_count` or `1` or `0`.
    """
    if bucket_count is None:
        bucket_count = DEFAULT_BUCKET_COUNT
    with tf.name_scope("buckets"):
        tf.debugging.assert_scalar(bucket_count)
        tf.debugging.assert_type(bucket_count, tf.int32)
        data = tf.reshape(data, shape=[-1])  # flatten
        data = tf.cast(data, tf.float64)
        is_empty = tf.equal(tf.size(input=data), 0)

        def when_empty():
            return tf.constant([], shape=(0, 3), dtype=tf.float64)

        def when_nonempty():
            min_ = tf.reduce_min(input_tensor=data)
            max_ = tf.reduce_max(input_tensor=data)
            range_ = max_ - min_
            is_singular = tf.equal(range_, 0)

            def when_nonsingular():
                bucket_width = range_ / tf.cast(bucket_count, tf.float64)
                offsets = data - min_
                bucket_indices = tf.cast(
                    tf.floor(offsets / bucket_width), dtype=tf.int32
                )
                clamped_indices = tf.minimum(bucket_indices, bucket_count - 1)
                # Use float64 instead of float32 to avoid accumulating floating point error
                # later in tf.reduce_sum when summing more than 2^24 individual `1.0` values.
                # See https://github.com/tensorflow/tensorflow/issues/51419 for details.
                one_hots = tf.one_hot(
                    clamped_indices, depth=bucket_count, dtype=tf.float64
                )
                bucket_counts = tf.cast(
                    tf.reduce_sum(input_tensor=one_hots, axis=0),
                    dtype=tf.float64,
                )
                edges = tf.linspace(min_, max_, bucket_count + 1)
                # Ensure edges[-1] == max_, which TF's linspace implementation does not
                # do, leaving it subject to the whim of floating point rounding error.
                edges = tf.concat([edges[:-1], [max_]], 0)
                left_edges = edges[:-1]
                right_edges = edges[1:]
                return tf.transpose(
                    a=tf.stack([left_edges, right_edges, bucket_counts])
                )

            def when_singular():
                center = min_
                bucket_starts = tf.stack([center - 0.5])
                bucket_ends = tf.stack([center + 0.5])
                bucket_counts = tf.stack(
                    [tf.cast(tf.size(input=data), tf.float64)]
                )
                return tf.transpose(
                    a=tf.stack([bucket_starts, bucket_ends, bucket_counts])
                )

            return tf.cond(is_singular, when_singular, when_nonsingular)

        return tf.cond(is_empty, when_empty, when_nonempty)


def histogram_pb(tag, data, buckets=None, description=None):
    """Create a histogram summary protobuf.

    Arguments:
      tag: String tag for the summary.
      data: A `np.array` or array-like form of any shape. Must have type
        castable to `float`.
      buckets: Optional positive `int`. The output will have this
        many buckets, except in two edge cases. If there is no data, then
        there are no buckets. If there is data but all points have the
        same value, then there is one bucket whose left and right
        endpoints are the same.
      description: Optional long-form description for this summary, as a
        `str`. Markdown is supported. Defaults to empty.

    Returns:
      A `summary_pb2.Summary` protobuf object.
    """
    bucket_count = DEFAULT_BUCKET_COUNT if buckets is None else buckets
    data = np.array(data).flatten().astype(float)
    if data.size == 0:
        buckets = np.array([]).reshape((0, 3))
    else:
        min_ = np.min(data)
        max_ = np.max(data)
        range_ = max_ - min_
        if range_ == 0:
            center = min_
            buckets = np.array([[center - 0.5, center + 0.5, float(data.size)]])
        else:
            bucket_width = range_ / bucket_count
            offsets = data - min_
            bucket_indices = np.floor(offsets / bucket_width).astype(int)
            clamped_indices = np.minimum(bucket_indices, bucket_count - 1)
            one_hots = np.array([clamped_indices]).transpose() == np.arange(
                0, bucket_count
            )  # broadcast
            assert one_hots.shape == (data.size, bucket_count), (
                one_hots.shape,
                (data.size, bucket_count),
            )
            bucket_counts = np.sum(one_hots, axis=0)
            edges = np.linspace(min_, max_, bucket_count + 1)
            left_edges = edges[:-1]
            right_edges = edges[1:]
            buckets = np.array(
                [left_edges, right_edges, bucket_counts]
            ).transpose()
    tensor = tensor_util.make_tensor_proto(buckets, dtype=np.float64)

    summary_metadata = metadata.create_summary_metadata(
        display_name=None, description=description
    )
    summary = summary_pb2.Summary()
    summary.value.add(tag=tag, metadata=summary_metadata, tensor=tensor)
    return summary
