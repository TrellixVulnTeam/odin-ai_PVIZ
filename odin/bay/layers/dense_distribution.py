from __future__ import absolute_import, annotations, division, print_function

import inspect
from functools import partial
from numbers import Number
from typing import Any, Callable, List, Optional, Text, Type, Union

import numpy as np
import tensorflow as tf
from odin import backend as bk
from odin.bay.helpers import (KLdivergence, is_binary_distribution,
                              is_discrete_distribution, is_mixture_distribution,
                              is_zeroinflated_distribution, kl_divergence)
from odin.bay.layers.deterministic_layers import VectorDeterministicLayer
from odin.bay.layers.distribution_util_layers import Moments, Sampling
from odin.networks import NetworkConfig
from odin.utils import as_tuple
from six import string_types
from tensorflow import Tensor
from tensorflow.python.keras import Model, Sequential
from tensorflow.python.keras.constraints import Constraint
from tensorflow.python.keras.initializers.initializers_v2 import Initializer
from tensorflow.python.keras.layers import Dense, Lambda, Layer
from tensorflow.python.keras.regularizers import Regularizer
from tensorflow_probability.python.bijectors import FillScaleTriL
from tensorflow_probability.python.distributions import (Categorical,
                                                         Distribution,
                                                         Independent,
                                                         MixtureSameFamily,
                                                         MultivariateNormalDiag,
                                                         MultivariateNormalTriL,
                                                         Normal)
from tensorflow_probability.python.internal import \
    distribution_util as dist_util
from tensorflow_probability.python.layers import DistributionLambda
from tensorflow_probability.python.layers.distribution_layer import (
    DistributionLambda, _get_convert_to_tensor_fn, _serialize,
    _serialize_function)
from typing_extensions import Literal

__all__ = [
    'DenseDeterministic',
    'DenseDistribution',
    'MixtureDensityNetwork',
    'MixtureMassNetwork',
    'DistributionNetwork',
]


# ===========================================================================
# Helpers
# ===========================================================================
def _params_size(layer, event_shape, **kwargs):
  spec = inspect.getfullargspec(layer.params_size)
  args = spec.args + spec.kwonlyargs
  if 'event_size' == args[0]:
    event_shape = tf.reduce_prod(event_shape)
  # extra kwargs from function closure
  kw = {}
  if len(args) > 1:
    fn = layer._make_distribution_fn
    closures = {
        k: v.cell_contents
        for k, v in zip(fn.__code__.co_freevars, fn.__closure__)
    }
    for k in args[1:]:
      if k in closures:
        kw[k] = closures[k]
  kw.update({k: v for k, v in kwargs.items() if k in spec.args})
  return layer.params_size(event_shape, **kw)


def _get_all_args(fn):
  spec = inspect.getfullargspec(fn)
  return spec.args + spec.kwonlyargs


# ===========================================================================
# Main classes
# ===========================================================================
class DenseDistribution(Dense):
  r""" Using `Dense` layer to parameterize the tensorflow_probability
  `Distribution`

  Arguments:
    event_shape : `int`
      number of output units.
    posterior : the posterior distribution, a distribution alias or Distribution
      type can be given for later initialization (Default: 'normal').
    prior : {`None`, `tensorflow_probability.Distribution`}
      prior distribution, used for calculating KL divergence later.
    use_bias : `bool` (default=`True`)
      enable biases for the Dense layers
    posterior_kwargs : `dict`. Keyword arguments for initializing the posterior
      `DistributionLambda`

  Return:
    `tensorflow_probability.Distribution`
  """

  def __init__(
      self,
      event_shape: List[int] = (),
      posterior: Union[str, DistributionLambda] = 'normal',
      posterior_kwargs: dict = {},
      prior: Optional[Union[Distribution, Callable[[], Distribution]]] = None,
      convert_to_tensor_fn: Callable[..., Tensor] = Distribution.sample,
      dropout: float = 0.0,
      activation: Union[str, Callable[..., Tensor]] = 'linear',
      use_bias: bool = True,
      kernel_initializer: Union[str, Initializer] = 'glorot_normal',
      bias_initializer: Union[str, Initializer] = 'zeros',
      kernel_regularizer: Union[str, Regularizer] = None,
      bias_regularizer: Union[str, Regularizer] = None,
      activity_regularizer: Union[str, Regularizer] = None,
      kernel_constraint: Union[str, Constraint] = None,
      bias_constraint: Union[str, Constraint] = None,
      projection: bool = True,
      **kwargs,
  ):
    assert isinstance(prior, (Distribution, Callable, type(None))), \
      ("prior can only be None or instance of Distribution, DistributionLambda"
       f",  but given: {prior}-{type(prior)}")
    # duplicated event_shape or event_size in posterior_kwargs
    posterior_kwargs = dict(posterior_kwargs)
    if 'event_shape' in posterior_kwargs:
      event_shape = posterior_kwargs.pop('event_shape')
    if 'event_size' in posterior_kwargs:
      event_shape = posterior_kwargs.pop('event_size')
    convert_to_tensor_fn = posterior_kwargs.pop('convert_to_tensor_fn',
                                                Distribution.sample)
    # process the posterior
    if inspect.isclass(posterior) and issubclass(posterior, DistributionLambda):
      post_layer_cls = posterior
    else:
      from odin.bay.distribution_alias import parse_distribution
      post_layer_cls, _ = parse_distribution(posterior)
    # create layers
    self._convert_to_tensor_fn = convert_to_tensor_fn
    self._posterior = posterior
    self._prior = prior
    self._event_shape = event_shape
    self._dropout = dropout
    # for initializing the posterior
    self._posterior_class = post_layer_cls
    self._posterior_kwargs = posterior_kwargs
    self._posterior_sample_shape = ()
    self._posterior_layer = None
    # set more descriptive name
    name = kwargs.pop('name', None)
    if name is None:
      name = f'dense_{posterior if isinstance(posterior, string_types) else posterior.__class__.__name__}'
    kwargs['name'] = name
    # params_size could be static function or method
    if not projection:
      self._params_size = 0
    else:
      self._params_size = int(
          _params_size(self.posterior_layer, event_shape,
                       **self._posterior_kwargs))
    self._projection = bool(projection)
    super(DenseDistribution,
          self).__init__(units=self._params_size,
                         activation=activation,
                         use_bias=use_bias,
                         kernel_initializer=kernel_initializer,
                         bias_initializer=bias_initializer,
                         kernel_regularizer=kernel_regularizer,
                         bias_regularizer=bias_regularizer,
                         activity_regularizer=activity_regularizer,
                         kernel_constraint=kernel_constraint,
                         bias_constraint=bias_constraint,
                         **kwargs)
    # store the distribution from last call
    self._most_recent_distribution = None
    if 'input_shape' in kwargs and not self.built:
      pass

  def build(self, input_shape) -> DenseDistribution:
    if self.projection and not self.built:
      super().build(input_shape)
    self.built = True
    return self

  @property
  def params_size(self) -> int:
    return self._params_size

  @property
  def projection(self) -> bool:
    return self._projection and self.params_size > 0

  @property
  def is_binary(self) -> bool:
    return is_binary_distribution(self.posterior_layer)

  @property
  def is_discrete(self) -> bool:
    return is_discrete_distribution(self.posterior_layer)

  @property
  def is_mixture(self) -> bool:
    return is_mixture_distribution(self.posterior_layer)

  @property
  def is_zero_inflated(self) -> bool:
    return is_zeroinflated_distribution(self.posterior_layer)

  @property
  def event_shape(self) -> List[int]:
    shape = self._event_shape
    if not (tf.is_tensor(shape) or isinstance(shape, tf.TensorShape)):
      shape = tf.nest.flatten(shape)
    return shape

  @property
  def event_size(self) -> int:
    return tf.cast(tf.reduce_prod(self._event_shape), tf.int32)

  @property
  def prior(self) -> Optional[Union[Distribution, Callable[[], Distribution]]]:
    return self._prior

  @prior.setter
  def prior(self,
            p: Optional[Union[Distribution, Callable[[],
                                                     Distribution]]] = None):
    self._prior = p

  def _sample_fn(self, dist):
    return dist.sample(sample_shape=self._posterior_sample_shape)

  @property
  def posterior_layer(self) -> DistributionLambda:
    if not isinstance(self._posterior_layer, DistributionLambda):
      if self._convert_to_tensor_fn == Distribution.sample:
        fn = self._sample_fn
      else:
        fn = self._convert_to_tensor_fn
      self._posterior_layer = self._posterior_class(self._event_shape,
                                                    convert_to_tensor_fn=fn,
                                                    **self._posterior_kwargs)
    return self._posterior_layer

  @property
  def posterior(self) -> Distribution:
    r""" Return the most recent parametrized distribution,
    i.e. the result from the last `call` """
    return self._most_recent_distribution

  @tf.function
  def sample(self, sample_shape=(), seed=None):
    r""" Sample from prior distribution """
    if self._prior is None:
      raise RuntimeError("prior hasn't been provided for the %s" %
                         self.__class__.__name__)
    return self.prior.sample(sample_shape=sample_shape, seed=seed)

  def call(self, inputs, training=None, sample_shape=(), **kwargs):
    # projection by Dense layer could be skipped by setting projection=False
    # NOTE: a 2D inputs is important here, but we don't want to flatten
    # automatically
    params = inputs
    # do not use tf.cond here, it infer the wrong shape when trying to build
    # the layer in Graph mode.
    if self.projection:
      params = super().call(params)
    # applying dropout
    if self._dropout > 0:
      params = bk.dropout(params, p_drop=self._dropout, training=training)
    # create posterior distribution
    self._posterior_sample_shape = sample_shape
    posterior = self.posterior_layer(params, training=training)
    self._most_recent_distribution = posterior
    # NOTE: all distribution has the method kl_divergence, so we cannot use it
    posterior.KL_divergence = KLdivergence(
        posterior, prior=self.prior,
        sample_shape=None)  # None mean reuse sampled data here
    return posterior

  def kl_divergence(self,
                    prior=None,
                    analytic=True,
                    sample_shape=1,
                    reverse=True):
    r""" KL(q||p) where `p` is the posterior distribution returned from last
    call

    Arguments:
      prior : instance of `tensorflow_probability.Distribution`
        prior distribution of the latent
      analytic : `bool` (default=`True`). Using closed form solution for
        calculating divergence, otherwise, sampling with MCMC
      reverse : `bool`. If `True`, calculate `KL(q||p)` else `KL(p||q)`
      sample_shape : `int` (default=`1`)
        number of MCMC sample if `analytic=False`

    Return:
      kullback_divergence : Tensor [sample_shape, batch_size, ...]
    """
    if prior is None:
      prior = self._prior
    assert isinstance(prior, Distribution), "prior is not given!"
    if self.posterior is None:
      raise RuntimeError(
          "DenseDistribution must be called to create the distribution before "
          "calculating the kl-divergence.")

    kullback_div = kl_divergence(q=self.posterior,
                                 p=prior,
                                 analytic=bool(analytic),
                                 reverse=reverse,
                                 q_sample=sample_shape)
    if analytic:
      kullback_div = tf.expand_dims(kullback_div, axis=0)
      if isinstance(sample_shape, Number) and sample_shape > 1:
        ndims = kullback_div.shape.ndims
        kullback_div = tf.tile(kullback_div, [sample_shape] + [1] * (ndims - 1))
    return kullback_div

  def log_prob(self, x):
    r""" Calculating the log probability (i.e. log likelihood) using the last
    distribution returned from call """
    return self.posterior.log_prob(x)

  def __repr__(self):
    return self.__str__()

  def __str__(self):
    if self.prior is None:
      prior = 'None'
    elif isinstance(self.prior, Distribution):
      prior = (
          f"<{self.prior.__class__.__name__} "
          f"batch:{self.prior.batch_shape} event:{self.prior.event_shape}>")
    else:
      prior = str(self.prior)
    posterior = self._posterior_class.__name__
    if not hasattr(self, 'input_shape'):
      inshape = None
      outshape = None
    else:
      inshape = self.input_shape
      outshape = self.output_shape
    return (f"<'{self.name}' proj:{self.projection} "
            f"in:{inshape} out:{outshape} event:{self.event_shape} "
            f"#params:{self.units} post:{posterior} prior:{prior} "
            f"dropout:{self._dropout:.2f} kw:{self._posterior_kwargs}>")

  def get_config(self):
    config = super().get_config()
    config['convert_to_tensor_fn'] = _serialize(self._convert_to_tensor_fn)
    config['event_shape'] = self._event_shape
    config['posterior'] = self._posterior
    config['prior'] = self._prior
    config['dropout'] = self._dropout
    config['posterior_kwargs'] = self._posterior_kwargs
    config['projection'] = self.projection
    return config


# ===========================================================================
# Shortcuts
# ===========================================================================
class MixtureDensityNetwork(DenseDistribution):
  r""" Mixture Density Network

  Mixture of Gaussian parameterized by neural network

  For arguments information: `odin.bay.layers.mixture_layers.MixtureGaussianLayer`
  """

  def __init__(
      self,
      units: int,
      n_components: int = 2,
      covariance: str = 'none',
      tie_mixtures: bool = False,
      tie_loc: bool = False,
      tie_scale: bool = False,
      loc_activation: Union[str, Callable] = 'linear',
      scale_activation: Union[str, Callable] = 'softplus1',
      convert_to_tensor_fn: Callable = Distribution.sample,
      use_bias: bool = True,
      dropout: float = 0.0,
      kernel_initializer: Union[str, Initializer, Callable] = 'glorot_uniform',
      bias_initializer: Union[str, Initializer, Callable] = 'zeros',
      kernel_regularizer: Union[str, Regularizer, Callable] = None,
      bias_regularizer: Union[str, Regularizer, Callable] = None,
      activity_regularizer: Union[str, Regularizer, Callable] = None,
      kernel_constraint: Union[str, Constraint, Callable] = None,
      bias_constraint: Union[str, Constraint, Callable] = None,
      **kwargs,
  ):
    self.covariance = covariance
    self.n_components = n_components
    super().__init__(event_shape=units,
                     posterior='mixgaussian',
                     posterior_kwargs=dict(n_components=int(n_components),
                                           covariance=str(covariance),
                                           loc_activation=loc_activation,
                                           scale_activation=scale_activation,
                                           tie_mixtures=bool(tie_mixtures),
                                           tie_loc=bool(tie_loc),
                                           tie_scale=bool(tie_scale)),
                     convert_to_tensor_fn=convert_to_tensor_fn,
                     dropout=dropout,
                     activation='linear',
                     use_bias=use_bias,
                     kernel_initializer=kernel_initializer,
                     bias_initializer=bias_initializer,
                     kernel_regularizer=kernel_regularizer,
                     bias_regularizer=bias_regularizer,
                     activity_regularizer=activity_regularizer,
                     kernel_constraint=kernel_constraint,
                     bias_constraint=bias_constraint,
                     **kwargs)

  def set_prior(self,
                loc=0.,
                log_scale=np.log(np.expm1(1)),
                mixture_logits=None):
    r""" Set the prior for mixture density network

    loc : Scalar or Tensor with shape `[n_components, event_size]`
    log_scale : Scalar or Tensor with shape
      `[n_components, event_size]` for 'none' and 'diag' component, and
      `[n_components, event_size*(event_size +1)//2]` for 'full' component.
    mixture_logits : Scalar or Tensor with shape `[n_components]`
    """
    event_size = self.event_size
    if self.covariance == 'diag':
      scale_shape = [self.n_components, event_size]
      fn = lambda l, s: MultivariateNormalDiag(loc=l,
                                               scale_diag=tf.nn.softplus(s))
    elif self.covariance == 'none':
      scale_shape = [self.n_components, event_size]
      fn = lambda l, s: Independent(Normal(loc=l, scale=tf.math.softplus(s)), 1)
    elif self.covariance == 'full':
      scale_shape = [self.n_components, event_size * (event_size + 1) // 2]
      fn = lambda l, s: MultivariateNormalTriL(
          loc=l, scale_tril=FillScaleTriL(diag_shift=1e-5)(tf.math.softplus(s)))
    #
    if isinstance(log_scale, Number) or tf.rank(log_scale) == 0:
      loc = tf.fill([self.n_components, self.event_size], loc)
    #
    if isinstance(log_scale, Number) or tf.rank(log_scale) == 0:
      log_scale = tf.fill(scale_shape, log_scale)
    #
    if mixture_logits is None:
      p = 1. / self.n_components
      mixture_logits = np.log(p / (1. - p))
    if isinstance(mixture_logits, Number) or tf.rank(mixture_logits) == 0:
      mixture_logits = tf.fill([self.n_components], mixture_logits)
    #
    loc = tf.cast(loc, self.dtype)
    log_scale = tf.cast(log_scale, self.dtype)
    mixture_logits = tf.cast(mixture_logits, self.dtype)
    self._prior = MixtureSameFamily(
        components_distribution=fn(loc, log_scale),
        mixture_distribution=Categorical(logits=mixture_logits),
        name="prior")
    return self


class MixtureMassNetwork(DenseDistribution):
  r""" Mixture Mass Network

  Mixture of NegativeBinomial parameterized by neural network
  """

  def __init__(
      self,
      event_shape: List[int] = (),
      n_components: int = 2,
      dispersion: str = 'full',
      inflation: str = 'full',
      tie_mixtures: bool = False,
      tie_mean: bool = False,
      mean_activation: Union[str, Callable] = 'softplus1',
      disp_activation: Union[str, Callable] = None,
      alternative: bool = False,
      zero_inflated: bool = False,
      convert_to_tensor_fn: Callable = Distribution.sample,
      dropout: float = 0.0,
      use_bias: bool = True,
      kernel_initializer: Union[str, Initializer, Callable] = 'glorot_uniform',
      bias_initializer: Union[str, Initializer, Callable] = 'zeros',
      kernel_regularizer: Union[str, Regularizer, Callable] = None,
      bias_regularizer: Union[str, Regularizer, Callable] = None,
      activity_regularizer: Union[str, Regularizer, Callable] = None,
      kernel_constraint: Union[str, Constraint, Callable] = None,
      bias_constraint: Union[str, Constraint, Callable] = None,
      **kwargs,
  ):
    self.n_components = n_components
    self.dispersion = dispersion
    self.zero_inflated = zero_inflated
    self.alternative = alternative
    super().__init__(event_shape=event_shape,
                     posterior='mixnb',
                     prior=None,
                     posterior_kwargs=dict(n_components=int(n_components),
                                           mean_activation=mean_activation,
                                           disp_activation=disp_activation,
                                           dispersion=dispersion,
                                           inflation=inflation,
                                           alternative=alternative,
                                           zero_inflated=zero_inflated,
                                           tie_mixtures=bool(tie_mixtures),
                                           tie_mean=bool(tie_mean)),
                     convert_to_tensor_fn=convert_to_tensor_fn,
                     dropout=dropout,
                     activation='linear',
                     use_bias=use_bias,
                     kernel_initializer=kernel_initializer,
                     bias_initializer=bias_initializer,
                     kernel_regularizer=kernel_regularizer,
                     bias_regularizer=bias_regularizer,
                     activity_regularizer=activity_regularizer,
                     kernel_constraint=kernel_constraint,
                     bias_constraint=bias_constraint,
                     **kwargs)


class DenseDeterministic(DenseDistribution):
  r""" Similar to `keras.Dense` layer but return a
  `tensorflow_probability.VectorDeterministic` distribution to represent
  the output, hence, making it compatible to the probabilistic framework.
  """

  def __init__(
      self,
      units: int,
      dropout: float = 0.0,
      activation: Union[str, Callable] = 'linear',
      use_bias: bool = True,
      kernel_initializer: Union[str, Initializer, Callable] = 'glorot_uniform',
      bias_initializer: Union[str, Initializer, Callable] = 'zeros',
      kernel_regularizer: Union[str, Regularizer, Callable] = None,
      bias_regularizer: Union[str, Regularizer, Callable] = None,
      activity_regularizer: Union[str, Regularizer, Callable] = None,
      kernel_constraint: Union[str, Constraint, Callable] = None,
      bias_constraint: Union[str, Constraint, Callable] = None,
      **kwargs,
  ):
    super().__init__(event_shape=int(units),
                     posterior='vdeterministic',
                     posterior_kwargs={},
                     prior=None,
                     convert_to_tensor_fn=Distribution.sample,
                     dropout=dropout,
                     activation=activation,
                     use_bias=use_bias,
                     kernel_initializer=kernel_initializer,
                     bias_initializer=bias_initializer,
                     kernel_regularizer=kernel_regularizer,
                     bias_regularizer=bias_regularizer,
                     activity_regularizer=activity_regularizer,
                     kernel_constraint=kernel_constraint,
                     bias_constraint=bias_constraint,
                     **kwargs)


class DistributionNetwork(Model):
  """A simple sequential network that will output a Distribution
  or multiple Distrubtions

  Parameters
  ----------
  distributions : List[Layer]
      List of output Layers that parameterize the Distrubtions
  network : Union[Layer, NetworkConfig], optional
      a network
  name : str, optional
      by default 'DistributionNetwork'
  """

  def __init__(
      self,
      distributions: List[Layer],
      network: Union[Layer, NetworkConfig] = NetworkConfig([128, 128],
                                                           flatten_inputs=True),
      name: str = 'DistributionNetwork',
  ):
    super().__init__(name=name)
    ## prepare the preprocessing layers
    if isinstance(network, NetworkConfig):
      network = network.create_network()
    assert isinstance(network, Layer), \
      f'network must be instance of keras.layers.Layer but given {network}'
    self.network = network
    ## prepare the output distribution
    from odin.bay.random_variable import RVmeta
    self.distributions = []
    for d in as_tuple(distributions):
      if isinstance(d, RVmeta):
        d = d.create_posterior()
      assert isinstance(d, Layer), \
        ('distributions must be a list of Layer that return Distribution '
         f'in call(), but given {d}')
      self.distributions.append(d)
    # others
    self.network_kws = _get_all_args(self.network.call)
    self.distributions_kws = [_get_all_args(d.call) for d in self.distributions]

  def build(self, input_shape) -> DistributionNetwork:
    super().build(input_shape)
    return self

  def preprocess(self, inputs, **kwargs):
    hidden = self.network(
        inputs, **{k: v for k, v in kwargs.items() if k in self.network_kws})
    return hidden

  def call(self, inputs, **kwargs):
    hidden = self.preprocess(inputs, **kwargs)
    # applying the distribution transformation
    outputs = []
    for dist, args in zip(self.distributions, self.distributions_kws):
      o = dist(hidden, **{k: v for k, v in kwargs.items() if k in args})
      outputs.append(o)
    return outputs[0] if len(outputs) == 1 else tuple(outputs)

  def __str__(self):
    from odin.backend.keras_helpers import layer2text
    shape = (self.network.input_shape
             if hasattr(self.network, 'input_shape') else None)
    s = f'[DistributionNetwork]{self.name}'
    s += f'\n input_shape:{shape}\n '
    s += '\n '.join(layer2text(self.network).split('\n'))
    s += '\n Distribution:'
    for d in self.distributions:
      s += f'\n  {d}'
    return s
