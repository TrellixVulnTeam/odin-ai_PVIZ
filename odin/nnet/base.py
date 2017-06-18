from __future__ import division, absolute_import, print_function

import inspect
import numbers
import warnings
from itertools import chain
from functools import wraps
from collections import OrderedDict
from contextlib import contextmanager
from abc import ABCMeta, abstractmethod
from six.moves import zip, range, cPickle
from six import add_metaclass, types, string_types

import numpy as np

from odin import backend as K
from odin.backend.role import (add_role, has_roles, Parameter, Variable,
                                Weight, Bias)
from odin.utils import as_tuple, uuid, cache_memory, is_number, is_string

import tensorflow as tf

from .model import InputDescriptor

# ===========================================================================
# Global NNOp manager
# ===========================================================================
__ALL_NNOPS = {}


def get_all_nnops():
    """ Return a dictionary of (name, nnops) for all created NNOp """
    return __ALL_NNOPS


def assign_new_nnops(nnops):
    if not isinstance(nnops, NNOp):
        raise ValueError("The new assigned NNOp must be instance of odin.nnet.NNOp "
                         ", but the given object has type: %s" % str(type(nnops)))
    name = nnops.name
    if name in get_all_nnops():
        raise RuntimeError("Another NNOp of type: '%s', and name: '%s' has "
                           "already existed." % (type(__ALL_NNOPS[name]), name))
    __ALL_NNOPS[name] = nnops

# ===========================================================================
# Context manager
# ===========================================================================
__ARGS_SCOPE_STACK = [{}]


def _get_current_arg_scope(nnops, ops_name):
    ops = __ARGS_SCOPE_STACK[-1]
    for name, scope in ops.iteritems():
        # first case, name is string
        if is_string(name):
            if ops_name in name or name == nnops.__class__.__name__ or \
            name == str(type(nnops)):
                return scope
        # specified a type
        elif isinstance(name, type) and name in inspect.getmro(type(nnops)):
            return scope
        # specified an object
        elif isinstance(name, NNOp) and type(name) == type(nnops):
            return scope
    return {}


@contextmanager
def arg_scope(applied_nnops, **kwargs):
    """Stores the default arguments for the given set of applied_nnops.

    For usage, please see examples at top of the file.

    Parameters
    ----------
    applied_nnops: List or tuple string, type, or NNOp
        a dictionary containing the current scope. When list_ops_or_scope is a
        dict, kwargs must be empty. When list_ops_or_scope is a list or tuple,
        then every op in it need to be decorated with @add_arg_scope to work.
    **kwargs: keyword=value that will define the defaults for each op in
        list_ops. All the ops need to accept the given set of arguments.

    Return
    ------
    the current_scope, which is a dictionary of {op: {arg: value}}

    Raises
    ------
    TypeError: if list_ops is not a list or a tuple.
    ValueError: if any op in list_ops has not be decorated with @add_arg_scope.
    """
    if isinstance(applied_nnops, dict):
        applied_nnops = applied_nnops.items()
    else:
        applied_nnops = as_tuple(applied_nnops)
    # ====== assign scope for each Ops ====== #
    nnops_scope = {}
    for ops in applied_nnops:
        scope = kwargs.copy()
        if is_string(ops) or isinstance(ops, type):
            nnops_scope[ops] = scope
        elif isinstance(ops, (tuple, list)) and len(ops) == 2:
            ops, add_scope = ops
            scope.update(dict(add_scope))
            nnops_scope[ops] = scope
        elif isinstance(ops, dict):
            if len(ops) > 1:
                raise ValueError("No Support for length > 1, in ops argument specification.")
            ops, add_scope = ops.items()[0]
            scope.update(dict(add_scope))
            nnops_scope[ops] = scope
        else:
            raise ValueError("Cannot parsing arguments scope for ops: %s" % str(ops))
    # ====== yield then reset ====== #
    __ARGS_SCOPE_STACK.append(nnops_scope)
    yield None
    __ARGS_SCOPE_STACK.pop()


def _nnops_initscope(func):
    """ Add this decorator to __init__ of any NNet Op """
    if not callable(func) or func.__name__ != '__init__':
        raise ValueError("_nnops_initscope can be only applied to __init__ "
                         "of NNOp instance.")
    # getting the default arguments to check user intentionally override
    # default argument.
    spec = inspect.getargspec(func)
    if 'self' != spec.args[0]:
        raise RuntimeError("'self' argument must be the first argument of __init__.")
    default_args = OrderedDict([(i, '__no_argument__') for i in spec.args])
    if spec.defaults is not None:
        for name, value in zip(spec.args[::-1], spec.defaults[::-1]):
            default_args[name] = value

    @wraps(func)
    def _wrap_init(*args, **kwargs):
        self_arg = kwargs['self'] if 'self' in kwargs else args[0]
        if not isinstance(self_arg, NNOp):
            raise ValueError("_nnops_initscope can be only applied to __init__ "
                             "of NNOp instance.")
        # get name of the NNOp
        ops_name = kwargs.get('name', None)
        if ops_name is None:
            ops_name = "%s_%s" % (self_arg.__class__.__name__, uuid())
        # update the new arguments into default arguments
        new_args = OrderedDict([(name, args[i]) if i < len(args)
            else (name, default)
            for i, (name, default) in enumerate(default_args.iteritems())])
        new_args.update(kwargs)
        new_args['name'] = ops_name
        # get current scope
        current_scope = _get_current_arg_scope(self_arg, ops_name)
        final_args = {}
        for name, val in new_args.iteritems():
            # override default argument by current scope
            if name in current_scope and \
            (name not in default_args or default_args[name] == val):
                final_args[name] = current_scope[name]
            else:
                final_args[name] = val
        # check if all arguments is specified
        if any(i == '__no_argument__' for i in final_args.itervalues()):
            raise RuntimeError("The argument with name '%s' hasn't been specified."
                % str([i for i, j in final_args.iteritems() if j == '__no_argument__']))
        return func(**final_args)
    return _wrap_init


# ===========================================================================
# Helper
# ===========================================================================
def _initialize_param(name, spec, shape):
    """ return a ndarray or trainable_variable """
    #####################################
    # 0. initializing function.
    if callable(spec):
        spec = spec(shape)
    elif is_number(spec):
        spec = np.full(shape=shape, fill_value=spec)
    #####################################
    # 1. Shared variable, just check the shape.
    if K.is_trainable_variable(spec):
        spec_shape = spec.get_shape().as_list()
        if shape is None:
            shape = spec_shape
        elif tuple(shape) != tuple(spec_shape):
            raise Exception('Require variable with shape=%s, but was given different '
                            'shape=%s, name:%s.' %
                            (str(shape), str(spec_shape), str(name)))
    #####################################
    # 2. expression, we can only check number of dimension.
    elif K.is_tensor(spec):
        # We cannot check the shape here, Theano expressions (even shared
        # variables) do not have a fixed compile-time shape. We can check the
        # dimensionality though.
        # Note that we cannot assign a name here. We could assign to the
        # `name` attribute of the variable, but the user may have already
        # named the variable and we don't want to override this.
        if shape is not None and spec.get_shape().ndims != len(shape):
            raise Exception("parameter with name=%s has %d dimensions, should be "
                            "%d" % (name, spec.ndim, len(shape)))
    #####################################
    # 3. numpy ndarray, create shared variable wraper for it.
    elif isinstance(spec, np.ndarray):
        if shape is not None and spec.shape != shape:
            raise RuntimeError("parameter with name=%s has shape %s, should be "
                               "%s" % (name, spec.shape, shape))
    #####################################
    # 5. Exception.
    else:
        raise RuntimeError("cannot initialize parameters: 'spec' is not "
                           "a numpy array, a Theano expression, or a "
                           "callable")
    return spec, shape


class NNConfig(object):

    def __init__(self, nnops):
        super(NNConfig, self).__init__()
        # name -> variables
        if not isinstance(nnops, NNOp):
            raise ValueError("nnops must be instance of odin.nnet.NNOp")
        self._nnops = nnops
        self._input_desc = InputDescriptor()
        self._variables = OrderedDict()

    @property
    def variables(self):
        """ Return the list of all TensorVariables attached to this Config"""
        return self._variables.values()

    @property
    def input(self):
        """ Return the list of all TensorVariables attached to this Config"""
        return self._input_desc.placeholder

    @property
    def input_shape(self):
        return self._input_desc.shape

    @property
    def input_shape_ref(self):
        return self._input_desc.shape_ref

    @property
    def input_desc(self):
        return self._input_desc

    def check_input_desc(self, inputs):
        inputs = as_tuple(inputs)
        # convert shape tuple to list of shape tuple
        if any(is_number(i) or i is None for i in inputs):
            inputs = (inputs,)
        # first time initialized the input description
        if len(self._input_desc) == 0:
            self._input_desc.set_variables(inputs)
            for i, j in enumerate(self._input_desc._desc):
                j._name = '%s_in%.2d' % (self._nnops.name, i)
        # mismatch input desctiption
        _ = InputDescriptor(inputs)
        if self._input_desc != _:
            raise ValueError("This NNConfiguration required inputs: %s, but was given: "
                            "%s." % (str(self._input_desc), str(_)))
        # automatic fetch placeholder to replace raw description
        inputs = [i if K.is_tensor(i) else None for i in inputs]
        # Don't create placeholders if user already gave the Input Tensor
        if any(i is None for i in inputs):
            inputs = [j if i is None else i
                      for i, j in zip(inputs, as_tuple(self.input))]
        return inputs

    def __getattr__(self, name):
        if name in self._variables:
            return self._variables[name]
        elif name not in self.__dict__:
            raise AttributeError('Cannot find attribute with name="%s", for NNOp '
                                 'with name="%s"' % (name, self._nnops.name))
        return super(NNConfig, self).__getattr__(name)

    def create_params(self, spec, shape, name, roles=[], nb_params=1):
        """
        Parameters
        ----------
        spec: variable, numpy.ndarray, function
            specification for initializing the weights
        shape: tuple, list
            expected shape for given variable
        name: str
            name for the variable
        nnops: NNOp
            parent operator of this parameters
        roles: odin.basic.Variable
            categories of this variable
        nb_params: int
            number of parameters that horizontally stacked into
            given `shape (e.g. nb_params=2, create 2 parameters with
            given `shape and horizontally stack them into 1 parameters)
            * do NOT support when `spec` is variable.
        """
        if not isinstance(roles, (tuple, list)):
            roles = [roles]
        shape = tuple(shape)  # convert to tuple if needed
        if any(d <= 0 for d in shape):
            raise ValueError((
                "Cannot create param with a non-positive shape dimension. "
                "Tried to create param with shape=%r, name=%r") %
                (shape, name))

        # ====== create parameters ====== #
        spec = as_tuple(spec, nb_params)
        spec = [_initialize_param(name, s, shape) for s in spec]
        # check shape returned
        shape = list(set([i[-1] for i in spec]))
        if len(shape) > 1:
            raise Exception('shape are inconsitent among all given "spec", the '
                            'created shape is: %s' % str(shape))
        shape = shape[0]
        # check spec returned
        spec = [i[0] for i in spec]
        if isinstance(spec[0], np.ndarray):
            spec = np.concatenate(spec, axis=-1)
            shape = spec.shape
            spec = K.variable(spec, name=name)
        elif K.is_trainable_variable(spec[0]):
            if nb_params > 1:
                spec = np.concatenate([K.get_value(i) for i in spec], axis=-1)
                shape = spec.shape
                spec = K.variable(spec, name=name)
            else:
                spec = spec[0]
        elif K.is_tensor(spec[0]):
            shape = (shape[0] * nb_params,) if len(shape) == 1 \
                else shape[:-1] + (shape[-1] * nb_params,)
            spec = tf.concat(spec, axis=-1)
        # ====== assign annotations ====== #
        # only add role for trainable variables
        for i in roles:
            if issubclass(i, Variable) and K.is_trainable_variable(spec):
                add_role(spec, i)
        # return actual variable or expression
        # override other parameters with same name
        self._variables[name] = spec
        return spec

    def __str__(self):
        s = ""
        for i in self._input_desc:
            s += str(i) + "\n"
        s += ' - Parameters: ' + ', '.join([str(i) for i in self._variables.values()])
        return s

    # ==================== pickling method ==================== #
    def __getstate__(self):
        return self._nnops, self._input_desc, \
        [(name, K.pickling_variable(var)) for name, var in self._variables.iteritems()]

    def __setstate__(self, states):
        self._nnops = states[0]
        self._input_desc = states[1]
        self._variables = OrderedDict([(name, K.pickling_variable(var))
                           for name, var in states[2]])


# ===========================================================================
# Main Ops
# ===========================================================================
@add_metaclass(ABCMeta)
class NNOp(object):
    """ Basics of all Neural Network operators

    Properties
    ----------
    name: str
        identity of the operator, this name is the scope for its operator
        and should be unique.
    T: NNOp
        transpose operator of this one (NOTE: some ops does not support
        transpose and raise NotImplementedError)
    parameters: list of variables
        list of all parameters associated with this operator scope

    Abstract
    --------
    _apply(self, x, **kwargs): resulted variables
        apply take a list of variables and custom parameters to compute
        output variables
    _initialize(self, x, **kwargs): NNConfig
        create and return NNConfig object, which is identity from
        other configuration

    Override
    --------
    _transpose(self): NNOp
        return another NNOp which is transposed version of this ops

    Note
    ----
    All NNOp are pickle-able!
    if NNOp is applied to a list of inputs, it will process each input seperated
    """

    def __init__(self, name=None, **kwargs):
        super(NNOp, self).__init__()
        self._save_states = {}

        if name is None:
            name = "%s_%s" % (self.__class__.__name__, uuid())
        elif not is_string(name):
            raise ValueError("name for NNOp must be string, but given name "
                             "has type: %s" % (name))
        self._name = str(name)

        self._configuration = NNConfig(self)
        self._transpose_ops = None
        self._is_initialized = False

    # ==================== pickling method ==================== #
    def __getstate__(self):
        return self._save_states

    def __setstate__(self, states):
        self._save_states = states
        for i, j in self._save_states.iteritems():
            setattr(self, i, j)
        # ====== check exist NNOp ====== #
        name = self.name
        if name in get_all_nnops():
            # compare 2 NNOp to make sure they are the same
            nnops = get_all_nnops()[name]
            if type(nnops) == type(self):
                for i, j in self._save_states.iteritems():
                    if i in nnops._save_states:
                        k = nnops._save_states[i]
                        if type(k) == type(j):
                            if K.is_tensor(j) and k.get_shape() != j.get_shape():
                                pass
                            else:
                                continue
                    raise RuntimeError("The pre-defined NNOp (%s) and the "
                        "new NNOp (%s) is different on the attribute: '%s'; "
                        "%s != %s." % (str(nnops), str(self), i, str(j), str(k)))
            else:
                raise RuntimeError("Found pre-defined NNOp of type=%s, and the "
                                   "new NNOp with type=%s." % (type(nnops), type(self)))
        elif self._is_initialized:
            assign_new_nnops(self)

    # ==================== properties ==================== #
    @property
    def name(self):
        return self._name

    @property
    def T(self):
        """ Return new ops which is transpose of this ops """
        if self._transpose_ops is None:
            self._transpose_ops = self._transpose()
            if not isinstance(self._transpose_ops, NNOp):
                raise ValueError("The _transposed method must return NNOp."
                                 "but the returned object has type=%s" %
                                 str(type(self._transpose_ops)))
        return self._transpose_ops

    @property
    def variables(self):
        if not self._is_initialized:
            raise Exception("This operators haven't initialized.")
        return self._configuration.variables

    @property
    def parameters(self):
        """ return all TensorVariables which have the PARAMETER role"""
        return [i for i in self.variables if has_roles(i, Parameter)]

    @property
    def trainable_variables(self):
        """ return all TensorVariables which are trainable """
        return [i for i in self.variables
                if K.is_trainable_variable(i)]

    @property
    def config(self):
        return self._configuration

    @property
    def is_initialized(self):
        return self._is_initialized

    @property
    def input(self):
        """ Create list of placeholder to represent inputs of this NNOp
        """
        return self._configuration.input

    @property
    def input_desc(self):
        return self._configuration._input_desc

    @property
    def nb_input(self):
        return len(self._configuration._input_desc)

    @property
    def input_shape(self):
        return self._configuration.input_shape

    @property
    def input_shape_ref(self):
        return self._configuration.input_shape_ref

    def __setattr__(self, name, value):
        # this record all assigned attribute to pickle them later
        # check hasattr to prevent recursive loop at the beginning before
        # __init__ is called
        if hasattr(self, '_save_states') and name != '_save_states':
            # otherwise, only save primitive types
            if isinstance(value, _PRIMITIVE_TYPES):
                self._save_states[name] = value
        return super(NNOp, self).__setattr__(name, value)

    def __getattr__(self, name):
        # merge the attributes of ops wit its configuration
        if name in self.__dict__:
            return self.__dict__[name]
        return getattr(self._configuration, name)

    # ==================== abstract method ==================== #
    def _initialize(self, **kwargs):
        """ This function is only called once, for the first time you
        apply this Ops
        """
        return None

    @abstractmethod
    def _apply(self, X, **kwargs):
        raise NotImplementedError

    def _transpose(self):
        raise NotImplementedError

    # ==================== interaction method ==================== #
    def apply(self, X, **kwargs):
        with tf.variable_scope(self.name, reuse=self.is_initialized):
            # ====== initialize first ====== #
            # only select necessary arguments
            argspec = inspect.getargspec(self._initialize)
            keywords = {}
            # kwargs must be specified in args, or the _initialize
            # must accept **kwaobject, class_or_type_or_tuplergs
            for i, j in kwargs.iteritems():
                if argspec.keywords is not None or i in argspec.args:
                    keywords[i] = j
            # initialize the operator (call the initilazation process)
            X = self._configuration.check_input_desc(X)
            if not self._is_initialized:
                self._initialize(**keywords)
                self._is_initialized = True
                # only assign new NNOp if it is initialized
                assign_new_nnops(self)
            # ====== calculate and return outputs ====== #
            rets = self._apply(X[0] if len(X) == 1 else X, **kwargs)
            return rets

    def __call__(self, X, **kwargs):
        return self.apply(X, **kwargs)

    def __str__(self):
        ops_format = '<ops: %s, name: %s, init: %s>'
        return ops_format % (self.__class__.__name__, self.name,
                             self._is_initialized)

    # ==================== Slicing ==================== #
    def __getitem__(self, key):
        return NNSliceOp(self, key)


_PRIMITIVE_TYPES = (tuple, list, dict, string_types, type(True),
                    types.FunctionType, numbers.Number, type(None),
                    K.rand.constant, NNConfig, NNOp)


# ===========================================================================
# Helper
# ===========================================================================
class NNSliceOp(NNOp):

    def __init__(self, ops, slice):
        if not isinstance(ops, NNOp):
            raise ValueError('ops must be instance of NNOp, but was given argument '
                             'has %s' % str(type(ops)))
        super(NNSliceOp, self).__init__()
        self._ops = ops
        if not isinstance(slice, (tuple, list)):
            slice = [slice]
        self.slice = slice

    @property
    def variables(self):
        return self._ops.variables

    def _apply(self, X, **kwargs):
        y = self._ops.apply(X, **kwargs)
        return_list = True if isinstance(y, (tuple, list)) else False
        # apply slice and calculate the shape
        output = [i[self.slice] for i in as_tuple(y)]
        # return output
        if return_list:
            return output
        return output[0]

    def __str__(self):
        ops_format = '<ops: %s, name: %s, init: %s, slice: %s>'
        return ops_format % (self._ops.__class__.__name__, self._ops.name,
                             self._ops.is_initialized, str(self.slice))


class NNTransposeOps(NNOp):
    """ TransposeOps
    Create a transposed view of the origin NNOp
    """

    def __init__(self, ops):
        super(NNTransposeOps, self).__init__(name=ops.name + '_transpose')
        if not isinstance(ops, NNOp):
            raise ValueError("NNTransposeOps can only be applied for instance of "
                             "odin.nnet.NNOp, but was given type=%s" % str(type(ops)))
        self._transpose_ops = ops

    def _transpose(self):
        # return original Ops to prevent infinite useless loop of transpose
        return self._transpose_ops

    def _initialize(self, **kwargs):
        if not self._transpose_ops.is_initialized:
            raise RuntimeError("The original NNOp with name:%s have not been "
                               "initialized, you must call the original NNOp "
                               "first." % self._ops)

    def __str__(self):
        ops_format = '<original_ops: %s, name: %s, init: %s>'
        return ops_format % (self._transpose_ops.__class__.__name__,
                             self.name, self._transpose_ops.is_initialized and
                             self.is_initialized)


# ===========================================================================
# Simple ops
# ===========================================================================
class Dense(NNOp):

    @_nnops_initscope
    def __init__(self, num_units,
                 W_init=K.rand.glorot_uniform,
                 b_init=K.rand.constant(0),
                 activation=K.linear,
                 **kwargs):
        super(Dense, self).__init__(**kwargs)
        self.activation = (K.linear if activation is None else activation)
        self.W_init = W_init
        self.b_init = b_init
        self.num_units = num_units

    # ==================== abstract methods ==================== #
    def _transpose(self):
        # create the new dense
        return TransposeDense(self)

    def _initialize(self):
        input_shape = self.input_shape
        shape = (input_shape[-1], self.num_units)
        self.config.create_params(self.W_init, shape, 'W', roles=Weight)
        if self.b_init is not None:
            self.config.create_params(self.b_init,
                shape=(self.num_units,), name='b', roles=Bias)

    def _apply(self, X):
        # calculate projection
        activation = K.dot(X, self.W)
        # add the bias
        if self.b_init is not None:
            activation = activation + self.b
        # Nonlinearity might change the shape of activation
        return self.activation(activation)


class TransposeDense(NNTransposeOps):

    def _initialize(self):
        super(TransposeDense, self)._initialize()
        self.num_units = self.T.input_shape[-1]
        if self.T.b_init is not None:
            self.config.create_params(self.T.b_init,
                shape=(self.num_units,), name='b', roles=Bias)

    def _apply(self, X):
        # calculate projection
        activation = K.dot(X, tf.transpose(self.T.W))
        if self.T.b_init is not None:
            activation = activation + self.b
        # Nonlinearity might change the shape of activation
        return self.T.activation(activation)


class ParametricRectifier(NNOp):
    """ This class is adpated from Lasagne:
    Original work Copyright (c) 2014-2015 lasagne contributors
    All rights reserved.
    LICENSE: https://github.com/Lasagne/Lasagne/blob/master/LICENSE
    A layer that applies parametric rectify activation to its input
    following [1]_ (http://arxiv.org/abs/1502.01852)
    Equation for the parametric rectifier linear unit:
    :math:`\\varphi(x) = \\max(x,0) + \\alpha \\min(x,0)`
    Parameters
    ----------
    incoming : a :class:`Layer` instance or a tuple
        The layer feeding into this layer, or the expected input shape
    alpha : Theano shared variable, expression, numpy array or callable
        Initial value, expression or initializer for the alpha values. The
        shape must match the incoming shape, skipping those axes the alpha
        values are shared over (see the example below).
        See :func:`lasagne.utils.create_params` for more information.
    shared_axes : 'auto', 'all', int or tuple of int
        The axes along which the parameters of the rectifier units are
        going to be shared. If ``'auto'`` (the default), share over all axes
        except for the second - this will share the parameter over the
        minibatch dimension for dense layers, and additionally over all
        spatial dimensions for convolutional layers. If ``'all'``, share over
        all axes, which corresponds to a single scalar parameter.
    **kwargs
        Any additional keyword arguments are passed to the `Layer` superclass.
     References
    ----------
    .. [1] K He, X Zhang et al. (2015):
       Delving Deep into Rectifiers: Surpassing Human-Level Performance on
       ImageNet Classification,
       http://link.springer.com/chapter/10.1007/3-540-49430-8_2
    Notes
    -----
    The alpha parameter dimensionality is the input dimensionality minus the
    number of axes it is shared over, which matches the same convention as
    the :class:`BiasLayer`.
    >>> layer = ParametricRectifierLayer((20, 3, 28, 28), shared_axes=(0, 3))
    >>> layer.alpha.get_value().shape
    (3, 28)
    """

    @_nnops_initscope
    def __init__(self, alpha_init=K.rand.constant(0.25),
                 shared_axes='auto', **kwargs):
        super(ParametricRectifier, self).__init__(**kwargs)
        self.alpha_init = alpha_init
        self.shared_axes = shared_axes

    # ==================== abstract methods ==================== #
    def _initialize(self):
        if self.shared_axes == 'auto':
            self.shared_axes = (0,) + tuple(range(2, len(self.input_shape)))
        elif self.shared_axes == 'all':
            self.shared_axes = tuple(range(len(self.input_shape)))
        elif isinstance(self.shared_axes, int):
            self.shared_axes = (self.shared_axes,)

        shape = [size for axis, size in enumerate(self.input_shape)
                 if axis not in self.shared_axes]
        if any(size is None for size in shape):
            raise ValueError("ParametricRectifierLayer needs input sizes for "
                             "all axes that alpha's are not shared over.")
        self.alpha = self.config.create_params(
            self.alpha_init, shape, name="alpha", roles=Parameter)

    def _apply(self, x):
        axes = iter(range(K.ndim(self.alpha)))
        pattern = ['x' if input_axis in self.shared_axes
                   else next(axes)
                   for input_axis in range(K.ndim(x))]
        alpha = K.dimshuffle(self.alpha, pattern)
        return K.relu(x, alpha)
