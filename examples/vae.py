"""VAE example from numpyro.

original: https://github.com/pyro-ppl/numpyro/blob/master/examples/vae.py
"""

import argparse
import os
import time

import matplotlib.pyplot as plt

import jax.numpy as np
from jax import jit, lax, random
from jax.experimental import optimizers, stax
from jax.random import PRNGKey

import numpyro.distributions as dist
from datasets import MNIST, load_dataset
from numpyro.handlers import param, sample
# from numpyro.svi import elbo, svi
from svi import per_sample_elbo, svi


def sigmoid(x):
    return 1 / (1 + np.exp(x))


# TODO: move to JAX
def _elemwise_no_params(fun, **kwargs):
    def init_fun(rng, input_shape): return input_shape, ()

    def apply_fun(params, inputs, rng=None): return fun(inputs, **kwargs)

    return init_fun, apply_fun


Sigmoid = _elemwise_no_params(sigmoid)


RESULTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__),
                              '.results'))
os.makedirs(RESULTS_DIR, exist_ok=True)


def encoder(hidden_dim, z_dim):
    """defines the encoder, i.e., the network taking as from observations to latent variables

    z is following a normal distribution, needs mean and varaince
    
    network structure:
    x -> dense layer of hidden_dim with softplus activation --> dense layer of z_dim ( = means/loc of z)
                                                            |-> dense layer of z_dim with (elementwise) exp() as activation func ( = variance of z )
    note(lumip): I believe the exp() as activation function is solely to ensure positivity of the variance

    :param hidden_dim: number of nodes in the hidden layer
    :param z_dim: dimension of the latent variable z
    :return: (init_fun, apply_fun) pair of the encoder: (encoder_init, encode)
    """
    return stax.serial(
        stax.Dense(hidden_dim, W_init=stax.randn()), stax.Softplus,
        stax.FanOut(2),
        stax.parallel(stax.Dense(z_dim, W_init=stax.randn()),
                      stax.serial(stax.Dense(z_dim, W_init=stax.randn()), stax.Exp)),
    )


def decoder(hidden_dim, out_dim):
    """defines the decoder, i.e., the network taking us from latent variables back to observations (or at least observation space)
    
    network structure:
    z -> dense layer of hidden_dim with softplus activation -> dense layer of out_dim with sigmoid activation

    :param hidden_dim: number of nodes in the hidden layer
    :param out_dim: dimensions of the observations

    :return: (init_fun, apply_fun) pair of the decoder: (decoder_init, decode)
    """
    return stax.serial(
        stax.Dense(hidden_dim, W_init=stax.randn()), stax.Softplus,
        stax.Dense(out_dim, W_init=stax.randn()), Sigmoid,
    )


def model(batch, decode, z_dim, **kwargs):
    """Defines the generative probabilistic model: p(x|z)p(z)

    The model is conditioned on the observed data
    
    :param batch: a batch of observations
    :param decode: function implementing the decoder (latent -> observations)
    :param z_dim: dimensions of the latent variable / code
    :param other keyword arguments' are accepted but ignored

    :return: (named) sample x from the model observation distribution p(x|z)p(z)
    """
    decoder_params = param('decoder', None) # advertise/register decoder parameters
    batch = np.reshape(batch, (batch.shape[0], -1)) # squash each data item into a one-dimensional array (preserving only the batch size on the first axis)
    z = sample('z', dist.Normal(np.zeros((z_dim,)), np.ones((z_dim,)))) # prior on z is N(0,I)
    img_loc = decode(decoder_params, z) # evaluate decoder (p(x|z)) on sampled z to get means for output bernoulli distribution
    x = sample('obs', dist.Bernoulli(img_loc), obs=batch) # outputs x are sampled from bernoulli distribution depending on z and conditioned on the observed data
    return x


def guide(batch, encode, **kwargs):
    """Defines the probabilistic guide for z (variational approximation to posterior): q(z) ~ p(z|q)

    :param batch: a batch of observations
    :param encode: function implementing the encoder (observations -> latent)
    :param other keyword arguments: are accepted but ignored
    :return: (named) sampled z from the variational (guide) distribution q(z)
    """
    encoder_params = param('encoder', None) # advertise/register encoder parameters
    batch = np.reshape(batch, (batch.shape[0], -1)) # squash each data item into a one-dimensional array (preserving only the batch size on the first axis)
    z_loc, z_std = encode(encoder_params, batch) # obtain mean and variance for q(z) ~ p(z|x) from encoder
    z = sample('z', dist.Normal(z_loc, z_std)) # z follows q(z)
    return z


@jit
def binarize(rng, batch):
    """Binarizes a batch of observations with values in [0,1] by sampling from a Bernoulli distribution and using the original observations as means
    
    Reason: this example assumes a Bernoulli distribution for the decoder output and thus requires inputs to be binary values as well.

    note(lumip): from an answer to a pyro github issue for similar VAE example code using MNIST ( https://github.com/pyro-ppl/pyro/issues/529#issuecomment-342670366 ):
    "be aware to only do this once, as repeated sampling of the data provides unfair regularization to the model and also inflates likelihood scores"
    which is not how binarize is currently used throughout this file, i.e., repeated sampling occurs. Is that intended? what is "unfair" regularization
    supposed to mean in that context, though?

    :param rng: rng seed key
    :param batch: Batch of data with continous values in interval [0, 1]
    :return: tuple(rng, binarized_batch).
    """
    return random.bernoulli(rng, batch).astype(batch.dtype)



def main(args):
    # obtaining model and training algorithms
    out_dim = 28*28
    encoder_init, encode = encoder(args.hidden_dim, args.z_dim)
    decoder_init, decode = decoder(args.hidden_dim, out_dim)
    opt_init, opt_update, get_params = optimizers.adam(args.learning_rate)
    # note(lumip): pyro has a ClippedAdam optimizer ( https://github.com/pyro-ppl/pyro/blob/dev/pyro/optim/clipped_adam.py ).
    #   That could be what we want but I haven't yet figured out whether that clips gradients per sample or in total.
    #   However, modifying the optimizer is probably the most reasonable thing to do to get per-sample clipped gradients
    #   to work as that would allow usage not only in numpyro but in principle all the way back to pytorch and pyro
    # note(lumip): after looking into it a bit more, a gradient clipping decorator for Optimizer would probably be a good idea
    #   i.e. GradientClippedOptimizer(Optimizer, min, max) that takes the gradients, clips them, and passes them on to given optimizer

    per_sample_loss = per_sample_elbo
    combined_loss = np.sum
    svi_init, svi_update, svi_eval = svi(model, guide, per_sample_loss, combined_loss, opt_init, opt_update, 
                                         get_params, encode=encode, decode=decode, z_dim=args.z_dim)
    svi_update = jit(svi_update)

    # preparing random number generators and loading data
    rng = PRNGKey(0)
    rng, rng_enc, rng_dec, rng_shuffle_train = random.split(rng, 4)
    train_init, train_fetch = load_dataset(MNIST, batch_size=args.batch_size, split='train')
    test_init, test_fetch = load_dataset(MNIST, batch_size=args.batch_size, split='test')

    # initializing model and training algorithms
    rng_shuffle_train, rng_train_init = random.split(rng_shuffle_train, 2)
    num_train, train_idx = train_init(rng=rng_train_init)
    _, encoder_params = encoder_init(rng_enc, (args.batch_size, out_dim))
    _, decoder_params = decoder_init(rng_dec, (args.batch_size, args.z_dim))
    params = {'encoder': encoder_params, 'decoder': decoder_params}

    # note(lumip): why these?
    rng, rng_binarize, svi_init_rng = random.split(rng, 3)
    sample_batch = binarize(rng_binarize, train_fetch(0, train_idx)[0])
    opt_state = svi_init(svi_init_rng, (sample_batch,), (sample_batch,), params)

    # functions for training tasks
    @jit
    def epoch_train(opt_state, rng):
        """Trains one epoch

        :param opt_state: current state of the optimizer
        :param rng: rng key

        :return: overall training loss over the epoch
        """
        def body_fn(i, val):
            loss_sum, opt_state, rng = val
            rng, rng_binarize, update_rng = random.split(rng, 3)
            batch = binarize(rng_binarize, train_fetch(i, train_idx)[0])
            loss, opt_state, rng = svi_update(i, opt_state, update_rng, (batch,), (batch,),)
            loss_sum += loss
            return loss_sum, opt_state, rng

        return lax.fori_loop(0, num_train, body_fn, (0., opt_state, rng))

    @jit
    def eval_test(opt_state, rng):
        """Evaluates current model state on test data.

        :param opt_state: current state of the optimizer
        :param rng: rng key

        :return: loss over the test split
        """
        def body_fn(i, val):
            loss_sum, rng = val
            rng, rng_binarize, eval_rng = random.split(rng, 3)
            batch = binarize(rng_binarize, test_fetch(i, test_idx)[0])
            loss = svi_eval(opt_state, eval_rng, (batch,), (batch,)) / len(batch)
            loss_sum += loss
            return loss_sum, rng

        loss, _ = lax.fori_loop(0, num_test, body_fn, (0., rng))
        loss = loss / num_test
        return loss

    def reconstruct_img(epoch, num_epochs, opt_state, rng):
        """Reconstructs an image for the given epoch

        Obtains a sample from the testing data set and passes it through the VAE. Stores the result as image file
        'epoch_{epoch}_recons.png' and the original input as 'epoch_{epoch}_original.png' in folder '.results'.

        :param epoch: Number of the current epoch
        :param num_epochs: Number of total epochs
        :param opt_state: Current state of the optimizer
        :param rng: rng key
        """
        assert(num_epochs > 0)
        img = test_fetch(0, test_idx)[0][0]
        plt.imsave(os.path.join(RESULTS_DIR, "epoch_{:0{}d}_original.png".format(epoch, (int(np.log10(num_epochs))+1))), img, cmap='gray')
        rng, rng_binarize = random.split(rng, 2)
        test_sample = binarize(rng_binarize, img)
        params = get_params(opt_state)
        z_mean, z_var = encode(params['encoder'], test_sample.reshape([1, -1]))
        z = dist.Normal(z_mean, z_var).sample(rng)
        img_loc = decode(params['decoder'], z).reshape([28, 28])
        plt.imsave(os.path.join(RESULTS_DIR, "epoch_{:0{}d}_recons.png".format(epoch, (int(np.log10(num_epochs))+1))), img_loc, cmap='gray')

    # main training loop
    for i in range(args.num_epochs):
        t_start = time.time()
        rng_shuffle_train, rng_train_init, rng_test_init = random.split(rng_shuffle_train, 3)
        num_train, train_idx = train_init(rng=rng_train_init)
        _, opt_state, rng = epoch_train(opt_state, rng)
        rng, rng_test, rng_recons = random.split(rng, 3)
        num_test, test_idx = test_init(rng=rng_test_init)
        test_loss = eval_test(opt_state, rng_test)
        reconstruct_img(i, args.num_epochs, opt_state, rng_recons)
        print("Epoch {}: loss = {} ({:.2f} s.)".format(i, test_loss, time.time() - t_start))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="parse args")
    parser.add_argument('-n', '--num-epochs', default=20, type=int, help='number of training epochs')
    parser.add_argument('-lr', '--learning-rate', default=1.0e-3, type=float, help='learning rate')
    parser.add_argument('-batch-size', default=128, type=int, help='batch size')
    parser.add_argument('-z-dim', default=50, type=int, help='size of latent')
    parser.add_argument('-hidden-dim', default=400, type=int, help='size of hidden layer in encoder/decoder networks')
    args = parser.parse_args()
    main(args)
