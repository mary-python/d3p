# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: © 2019- d3p Developers and their Assignees

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

""" Stochastic Variational Inference implementation with per-example gradient
    manipulation capability.
"""
import functools
from typing import Any, NamedTuple, Sequence, Tuple

import jax
import jax.numpy as jnp
import numpy as np

from numpyro.infer.svi import SVI, SVIState
from numpyro.infer.elbo import ELBO
from numpyro.handlers import seed, trace, substitute, block

from d3p.util import example_count
import d3p.random as strong_rng
import d3p.random._internal_jax_rng_wrapper as jax_rng_wrapper

from fourier_accountant.compute_eps import get_epsilon_R
from fourier_accountant.compute_delta import get_delta_R

PRNGState = Any


class DPSVIState(NamedTuple):
    optim_state: Any
    rng_key: PRNGState
    observation_scale: float


def get_observations_scale(model, model_args, model_kwargs, params):
    """
    Traces through a model to extract the scale applied to observation log-likelihood.
    """

    # todo(lumip): is there a way to avoid tracing through the entire model?
    #       need to experiment with effect handlers and what exactly blocking achieves
    model = substitute(seed(model, 0), data=params)
    model = block(model, lambda msg: msg['type'] != 'sample' or not msg['is_observed'])
    model_trace = trace(model).get_trace(*model_args, **model_kwargs)
    scales = np.unique(
        [msg['scale'] if msg['scale'] is not None else 1 for msg in model_trace.values()]
    )

    if len(scales) > 1:
        raise ValueError(
            "The model received several observation sites with different example counts."
            " This is not supported in DPSVI."
        )
    elif len(scales) == 0:
        return 1.

    return scales[0]


class CombinedLoss(object):

    def __init__(self, per_example_loss: ELBO, combiner_fn=jnp.mean):
        self.px_loss = per_example_loss
        self.combiner_fn = combiner_fn

    def loss(self, rng_key, param_map, model, guide, *args, **kwargs):
        return self.combiner_fn(self.px_loss.loss(
            rng_key, param_map, model, guide, *args, **kwargs
        ))


def full_norm(list_of_parts_or_tree, ord=2):
    """Computes the total norm over a list of values (of any shape) or a jax
    tree by treating them as a single large vector.

    :param list_of_parts_or_tree: The list or jax tree of values that make up
        the vector to compute the norm over.
    :param ord: Order of the norm. May take any value possible for
    `numpy.linalg.norm`.
    :return: The indicated norm over the full vector.
    """
    if isinstance(list_of_parts_or_tree, list):
        list_of_parts = list_of_parts_or_tree
    else:
        list_of_parts = jax.tree_leaves(list_of_parts_or_tree)

    if list_of_parts is None or len(list_of_parts) == 0:
        return 0.

    ravelled = [g.ravel() for g in list_of_parts]
    gradients = jnp.concatenate(ravelled)
    assert(len(gradients.shape) == 1)
    norm = jnp.linalg.norm(gradients, ord=ord)
    return norm


def normalize_gradient(list_of_gradient_parts, ord=2):
    """Normalizes a gradient by its total norm.

    The norm is computed by interpreting the given list of parts as a single
    vector (see `full_norm`).

    :param list_of_gradient_parts: A list of values (of any shape) that make up
        the overall gradient vector.
    :return: Normalized gradients given in the same format/layout/shape as
        list_of_gradient_parts.
    """
    norm_inv = 1./full_norm(list_of_gradient_parts, ord=ord)
    normalized = [norm_inv * g for g in list_of_gradient_parts]
    return normalized


def clip_gradient(list_of_gradient_parts, c, rescale_factor=1.):
    """Clips the total norm of a gradient by a given value C.

    The norm is computed by interpreting the given list of parts as a single
    vector (see `full_norm`). Each entry is then scaled by the factor
    (1/max(1, norm/C)) which effectively clips the norm to C. Additionally,
    the gradient can be scaled by a given factor before clipping.

    :param list_of_gradient_parts: A list of values (of any shape) that make up
        the overall gradient vector.
    :param c: The clipping threshold C.
    :param rescale_factor: Factor to scale the gradient by before clipping.
    :return: Clipped gradients given in the same format/layout/shape as
        list_of_gradient_parts.
    """
    if c <= 0.:
        raise ValueError("The clipping threshold must be greater than 0.")
    norm = full_norm(list_of_gradient_parts) * rescale_factor  # norm of rescale_factor * grad
    normalization_constant = 1./jnp.maximum(1., norm/c)
    f = rescale_factor * normalization_constant  # to scale grad to max(rescale_factor * grad, C)
    clipped_grads = [f * g for g in list_of_gradient_parts]
    return clipped_grads


def get_gradients_clipping_function(c, rescale_factor):
    """Factory function to obtain a gradient clipping function for a fixed
    clipping threshold C.

    :param c: The clipping threshold C.
    :param rescale_factor: Factor to scale the gradient by before clipping.
    :return: `clip_gradient` function with fixed threshold C. Only takes a
        list_of_gradient_parts as argument.
    """
    @functools.wraps(clip_gradient)
    def gradient_clipping_fn_inner(list_of_gradient_parts):
        return clip_gradient(list_of_gradient_parts, c, rescale_factor)
    return gradient_clipping_fn_inner


class DPSVI(SVI):
    """
    Differentially-Private Stochastic Variational Inference [1] given a per-example
    loss objective and a gradient clipping threshold.

    This is identical to numpyro's `SVI` but adds differential privacy by
    clipping gradients per example to the given clipping_threshold and
    perturbing the batch gradient with noise determined by sigma*clipping_threshold.

    To obtain the per-example gradients, the `per_example_loss_fn` is evaluated
    for (and the gradient take wrt) each example in a vectorized manner (using
    `jax.vmap`).

    For this to work `per_example_loss_fn` must be able to deal with batches
    of single examples. The leading batch dimension WILL NOT be stripped away,
    however, so a `per_example_loss_fn` that can deal with arbitrarily sized batches
    suffices. Take special care that the loss function scales the likelihood
    contribution of the data properly wrt to batch size and total example count
    (use e.g. the `numpyro.scale` or the convenience `minibatch` context managers
    in the `model` and `guide` functions where appropriate).

    The user can also provide `pre_clipping_noise_scale` to apply additional
    perturbation before clipping the per-example gradients to mitigate
    potential clipping-induced bias, as proposed in [2].

    [1]: Jälkö, Dikmen, Honkela: Differentially Private Variational Inference for Non-conjugate Models
        https://arxiv.org/abs/1610.08749

    [2]: Chen, Wu, Hong: Understanding Gradient Clipping in Private SGD: A Geometric Perspective
        https://arxiv.org/abs/2006.15429

    :param model: Python callable with Pyro primitives for the model.
    :param guide: Python callable with Pyro primitives for the guide
        (recognition network).
    :param per_example_loss_fn: ELBo loss, i.e. negative Evidence Lower Bound,
        to minimize, per example.
    :param optim: an instance of :class:`~numpyro.optim._NumPyroOptim`.
    :param clipping_threshold: The clipping threshold C to which the norm
        of each per-example gradient is clipped.
    :param dp_scale: Scale parameter for the Gaussian mechanism applied to
        each dimension of the batch gradients.
    :param rng_suite:
    :param pre_clipping_noise_scale: Scale parameter for Gaussian noise applied
        to per-example gradients BEFORE clipping to mitigate clipping-induced bias.
        Leave as None to perform no pre-clipping perturbation.
    :param static_kwargs: static arguments for the model / guide, i.e. arguments
        that remain constant during fitting.
    """

    def __init__(
            self,
            model,
            guide,
            optim,
            per_example_loss,
            clipping_threshold,
            dp_scale,
            rng_suite=strong_rng,
            pre_clipping_noise_scale=None,
            **static_kwargs
        ):  # noqa: E121, E125

        self._clipping_threshold = clipping_threshold
        self._pre_clipping_noise_scale = pre_clipping_noise_scale
        self._dp_scale = dp_scale
        self._rng_suite = rng_suite

        if (not np.isfinite(clipping_threshold)):
            raise ValueError("clipping_threshold must be finite!")

        total_loss = CombinedLoss(per_example_loss, combiner_fn=jnp.mean)
        super().__init__(model, guide, optim, total_loss, **static_kwargs)

    @staticmethod
    def _update_state_rng(dp_svi_state: DPSVIState, rng_key: PRNGState) -> DPSVIState:
        return DPSVIState(
            dp_svi_state.optim_state,
            rng_key,
            dp_svi_state.observation_scale
        )

    @staticmethod
    def _update_state_optim_state(dp_svi_state: DPSVIState, optim_state: Any) -> DPSVIState:
        return DPSVIState(
            optim_state,
            dp_svi_state.rng_key,
            dp_svi_state.observation_scale
        )

    def _split_rng_key(self, dp_svi_state: DPSVIState, count: int=1) -> Tuple[DPSVIState, Sequence[PRNGState]]:
        rng_key = dp_svi_state.rng_key
        split_keys = self._rng_suite.split(rng_key, count+1)
        return DPSVI._update_state_rng(dp_svi_state, split_keys[0]), split_keys[1:]

    def init(self, rng_key, *args, **kwargs):
        jax_rng_key = self._rng_suite.convert_to_jax_rng_key(rng_key)
        svi_state = super().init(jax_rng_key, *args, **kwargs)

        if svi_state.mutable_state is not None:
            raise RuntimeError("Mutable state is not supported.")

        model_kwargs = dict(kwargs)
        model_kwargs.update(self.static_kwargs)

        one_element_batch = [
            jnp.expand_dims(a[0], 0) for a in args
        ]

        # note: DO use super().get_params here to get constrained/transformed params
        #  for use in get_observations_scale (svi_state.optim_state holds unconstrained params)
        params = super().get_params(svi_state)
        observation_scale = get_observations_scale(
            self.model, one_element_batch, model_kwargs, params
        )

        return DPSVIState(svi_state.optim_state, rng_key, observation_scale)

    def _compute_per_example_gradients(self, dp_svi_state, step_rng_key, *args, **kwargs):
        """ Computes the raw per-example gradients of the model.

        This is the first step in a full update iteration.

        :param dp_svi_state: The current state of the DPSVI algorithm.
        :param step_rng_key: RNG key for this step.
        :param args: Arguments to the loss function.
        :param kwargs: All keyword arguments to model or guide.
        :returns: tuple consisting of the updated DPSVI state, an array of loss
            values per example, and a jax tuple tree of per-example gradients
            per parameter site (each site's gradients have shape (batch_size, *parameter_shape))
        """
        jax_rng_key = self._rng_suite.convert_to_jax_rng_key(step_rng_key)

        # note: do NOT use self.get_params here; that applies constraint transforms for end-consumers of the parameters
        # but internally we maintain and optimize on unconstrained params
        # (they are constrained in the loss function so that we get the correct
        # effect of the constraint transformation in the gradient)
        params = self.optim.get_params(dp_svi_state.optim_state)

        # we wrap the per-example loss (ELBO) to make it easier "digestable"
        # for jax.vmap(jax.value_and_grad()): slighly reordering parameters; fixing kwargs, model and guide
        def wrapped_px_loss(prms, rng_key, loss_args):
            # vmap removes leading dimensions, we re-add those in a wrapper for fun so
            # that fun can be oblivious of this
            new_args = (jnp.expand_dims(arg, 0) for arg in loss_args)
            return self.loss.px_loss.loss(
                rng_key, self.constrain_fn(prms), self.model, self.guide,
                *new_args, **kwargs, **self.static_kwargs
            )

        batch_size = jnp.shape(args[0])[0]  # todo: need checks to ensure this indexing is okay
        px_rng_keys = jax.random.split(jax_rng_key, batch_size)

        px_value_and_grad = jax.vmap(jax.value_and_grad(wrapped_px_loss), in_axes=(None, 0, 0))
        per_example_loss, per_example_grads = px_value_and_grad(params, px_rng_keys, args)

        return dp_svi_state, per_example_loss, per_example_grads

    def _clip_gradients(self, dp_svi_state, step_rng_key, px_gradients, batch_size):
        """ Clips each per-example gradient.

        This is the second step in a full update iteration.

        :param dp_svi_state: The current state of the DPSVI algorithm.
        :param step_rng_key: RNG key for this step.
        :param px_gradients: Jax tuple tree of per-example gradients as returned
            by `_compute_per_example_gradients`
        :param batch_size: Size of the training batch.
        :returns: tuple consisting of the updated svi state, a list of
            transformed per-example gradients per site and the jax tree structure
            definition. The list is a flattened representation of the jax tree,
            the shape of per-example gradients per parameter is unaffected.
        """
        obs_scale = dp_svi_state.observation_scale

        # px_gradients is a jax tree of jax jnp.arrays of shape
        #   [batch_size, *param_shape] for each parameter. flatten it out!
        px_grads_list, px_grads_tree_def = jax.tree_flatten(
            px_gradients
        )

        # if a pre-clipping noise scale was provided we will perturb the per-example gradients
        #  before clipping.
        if self._pre_clipping_noise_scale is not None:
            clip_perturbation_rng = step_rng_key
            clip_perturbation_jax_rng = self._rng_suite.convert_to_jax_rng_key(clip_perturbation_rng)
            clip_perturbation_jax_rng = jax_rng_wrapper.split(clip_perturbation_jax_rng, batch_size)

            def px_pre_clipping_fn(px_grad, rng):
                return self.perturbation_function(
                    jax_rng_wrapper, rng, px_grad, self._pre_clipping_noise_scale
                )

            px_grads_list = jax.vmap(px_pre_clipping_fn, in_axes=0)(px_grads_list, clip_perturbation_jax_rng)

        # scale the gradients by 1/obs_scale then clip them:
        #  in the loss, every single examples loss contribution is scaled by obs_scale
        #  but the clipping threshold assumes no scaling.
        #  we scale by the reciprocal to ensure that clipping is correct.
        clip_fn = get_gradients_clipping_function(self._clipping_threshold, 1./obs_scale)
        px_grads_list = jax.vmap(clip_fn, in_axes=0)(px_grads_list)

        return dp_svi_state, px_grads_list, px_grads_tree_def

    def _combine_gradients(self, px_grads_list, px_loss):
        """ Combines the per-example gradients into the batch gradient and
            applies the batch gradient transformation given as
            `batch_grad_manipulation_fn`.

        This is the third step of a full update iteration.

        :param px_grads_list: List of transformed per-example gradients as returned
            by `_apply_per_example_gradient_transformations`
        :param px_loss: Array of per-example loss values as output by
            `_compute_per_example_gradients`.
        :returns: tuple consisting of the updated svi state, the loss value for
            the batch and a jax tree of batch gradients per parameter site.
        """

        assert(self.loss.combiner_fn == jnp.mean)

        loss_val = jnp.mean(px_loss, axis=0)
        grads_list = tuple(map(lambda px_grad_site: jnp.mean(px_grad_site, axis=0), px_grads_list))

        return loss_val, grads_list

    def _perturb_and_reassemble_gradients(self, dp_svi_state, step_rng_key, gradient_list, batch_size, px_grads_tree_def):
        """ Perturbs the gradients using Gaussian noise and reassembles the gradient tree.

        This is the fourth step of a full update iteration.

        :param dp_svi_state: The current state of the DPSVI algorithm.
        :param step_rng_key: RNG key for this step.
        :param gradient_list: List of batch gradients for each parameter site
        :param batch_size: Size of the training batch.
        :param px_grads_tree_def: Jax tree definition for the gradient tree as
            returned by `_apply_per_example_gradient_transformations`.
        """
        perturbation_scale = self._dp_scale * self._clipping_threshold / batch_size
        perturbed_grads_list = self.perturbation_function(
            self._rng_suite, step_rng_key, gradient_list, perturbation_scale
        )

        # we multiply each parameter site by obs_scale to revert the downscaling
        # performed before clipping, so that the final gradient is scaled as
        # expected without DP
        obs_scale = dp_svi_state.observation_scale
        perturbed_grads_list = tuple(
            grad * obs_scale
            for grad in perturbed_grads_list
        )

        # reassemble the jax tree used by optimizer for the final gradients
        perturbed_grads = jax.tree_unflatten(
            px_grads_tree_def, perturbed_grads_list
        )

        return dp_svi_state, perturbed_grads

    def _apply_gradient(self, dp_svi_state, batch_gradient):
        """ Takes a (batch) gradient step in parameter space using the specified
            optimizer.

        This is the fifth and last step of a full update iteration.
        :param dp_svi_state: The current state of the DPSVI algorithm.
        :param batch_gradient: Jax tree of batch gradients per parameter site,
            as returned by `_combine_and_transform_gradient`.
        :returns: tuple consisting of the updated svi state.
        """
        optim_state = dp_svi_state.optim_state
        new_optim_state = self.optim.update(batch_gradient, optim_state)

        dp_svi_state = self._update_state_optim_state(dp_svi_state, new_optim_state)
        return dp_svi_state

    def update(self, svi_state, *args, **kwargs):

        svi_state, update_rng_keys = self._split_rng_key(svi_state, 3)
        gradient_rng_key, perturbation_rng_key, pre_clipping_rng_key = update_rng_keys

        svi_state, per_example_loss, per_example_grads = \
            self._compute_per_example_gradients(svi_state, gradient_rng_key, *args, **kwargs)

        batch_size = example_count(per_example_loss)

        svi_state, per_example_grads, tree_def = \
            self._clip_gradients(
                svi_state, pre_clipping_rng_key, per_example_grads, batch_size
            )

        loss, gradient = self._combine_gradients(
            per_example_grads, per_example_loss
        )

        svi_state, gradient = self._perturb_and_reassemble_gradients(
            svi_state, perturbation_rng_key, gradient, batch_size, tree_def
        )

        svi_state = self._apply_gradient(svi_state, gradient)

        return svi_state, loss

    def evaluate(self, svi_state: DPSVIState, *args, **kwargs):
        """
        Take a single step of SVI (possibly on a batch / minibatch of data).

        :param svi_state: current state of DPSVI.
        :param args: arguments to the model / guide (these can possibly vary during
            the course of fitting).
        :param kwargs: keyword arguments to the model / guide.
        :return: evaluate ELBO loss given the current parameter values
            (held within `svi_state.optim_state`).
        """
        # we split to have the same seed as `update_fn` given an svi_state
        jax_rng_key = self._rng_suite.convert_to_jax_rng_key(self._rng_suite.split(svi_state.rng_key, 1)[0])
        numpyro_svi_state = SVIState(svi_state.optim_state, None, jax_rng_key)
        return super().evaluate(numpyro_svi_state, *args, **kwargs)

    def _validate_epochs_and_iter(self, num_epochs, num_iter, q):
        if num_epochs is not None:
            num_iter = num_epochs / q
        if num_iter is None:
            raise ValueError("A value must be supplied for either num_iter or num_epochs")
        return num_iter

    def get_epsilon(self, target_delta, q, num_epochs=None, num_iter=None):
        num_iter = self._validate_epochs_and_iter(num_epochs, num_iter, q)

        eps = get_epsilon_R(target_delta, self._dp_scale, q, ncomp=num_iter)
        return eps

    def get_delta(self, target_epsilon, q, num_epochs=None, num_iter=None):
        num_iter = self._validate_epochs_and_iter(num_epochs, num_iter, q)

        eps = get_delta_R(target_epsilon, self._dp_scale, q, ncomp=num_iter)
        return eps

    @staticmethod
    def perturbation_function(
            rng_suite, rng: PRNGState, values: Sequence[jnp.ndarray], perturbation_scale: float
        ) -> Sequence[jnp.ndarray]:  # noqa: E121, E125
        """ Perturbs given values using Gaussian noise.

        `values` can be a list of array-like objects. Each value is independently
        perturbed by adding noise sampled from a Gaussian distribution with a
        standard deviation of `perturbation_scale`.

        :param rng: Jax PRNGKey for perturbation randomness.
        :param values: Iterable of array-like where each value will be perturbed.
        :param perturbation_scale: The scale/standard deviation of the noise
            distribution.
        """
        def perturb_one(a: jnp.ndarray, site_rng: PRNGState) -> jnp.ndarray:
            """ perturbs a single gradient site """
            noise = rng_suite.normal(site_rng, a.shape) * perturbation_scale
            return a + noise

        per_site_rngs = rng_suite.split(rng, len(values))
        values = tuple(
            perturb_one(grad, site_rng)
            for grad, site_rng in zip(values, per_site_rngs)
        )
        return values
