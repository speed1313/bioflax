import jax
import jax.numpy as jnp
import optax
import matplotlib.pyplot as plt
import torch
import numpy as np
import flax.linen as nn
import flax
import jax.tree_util as jax_tree
from typing import Any
from jax.nn import one_hot
from tqdm import tqdm
from flax.training import train_state
from functools import partial
from .metric_computation import (
    compute_metrics,
    summarize_metrics_epoch,
    reorganize_dict
)


def create_train_state(model, rng, lr, momentum, weight_decay, in_dim, batch_size, seq_len):
    """
    Initializes the training state using optax
    ...
    Parameters
    __________
    model : Any
        model to be trained
    rng : jnp.PRNGKey
        key for randomness
    lr : float
        learning rate for optimizer
    momentum : float
        momentum for optimizer
    in_dim : int
        input dimension of model
    batch_size : int
        batch size used when running model
    seq_len : int
        sequence length used when running model
    """
    dummy_input = jnp.ones((batch_size, in_dim, seq_len))
    params = model.init(rng, dummy_input)['params']
    sgd_optimizer = optax.sgd(learning_rate=lr, momentum=momentum)
    tx = optax.chain(
        optax.add_decayed_weights(weight_decay),
        sgd_optimizer
    )
    return train_state.TrainState.create(apply_fn=model.apply, params=params, tx=tx)


def train_epoch(state, model, trainloader, loss_function, n, mode, compute_alignments):
    """
    Training function for an epoch that loops over batches.
    ...
    Parameters
    __________
    state : TrainState
        current train state of the model
    model : BioBeuralNetwork
        model that is trained
    seq_len : int
        length of a single input
    in_dim
        dimensionality of a single input
    loss_function: str
        identifier to select loss function
    n : int
        number of batches over which to compute alignments
    mode : str
        identifier for training mode
    compute_alignments : bool
        flag to compute alignments

    """
    batch_losses = []
    bias_als_per_layer = []
    wandb_grad_als_per_layer = []
    wandb_grad_als_total = []
    rel_norms_grads = []
    weight_als_per_layer = []

    for i, batch in enumerate(tqdm(trainloader)):
        inputs, labels = prep_batch(batch)

        if i < n and compute_alignments:
            def loss_comp(params):
                logits = model.apply({'params': params}, inputs)
                loss = get_loss(loss_function, logits, labels)
                return loss
            if (mode != 'bp'):
                _, grads_ = jax.value_and_grad(loss_comp)(
                    reorganize_dict({'params': state.params})["params"])
            else:
                _, grads_ = jax.value_and_grad(loss_comp)(state.params)

        state, loss, grads = train_step(state, inputs, labels, loss_function)
        batch_losses.append(loss)

        if i < n and compute_alignments:
            bias_al_per_layer, wandb_grad_al_per_layer, wandb_grad_al_total, weight_al_per_layer, rel_norm_grads = compute_metrics(
                state, grads_,  grads, mode)

            bias_als_per_layer.append(bias_al_per_layer)
            wandb_grad_als_per_layer.append(wandb_grad_al_per_layer)
            wandb_grad_als_total.append(wandb_grad_al_total)
            weight_als_per_layer.append(weight_al_per_layer)
            rel_norms_grads.append(rel_norm_grads)

    if compute_alignments:
        avg_bias_al_per_layer, avg_wandb_grad_al_per_layer, avg_wandb_grad_al_total, avg_weight_al_per_layer, avg_rel_norm_grads = summarize_metrics_epoch(
            bias_als_per_layer, wandb_grad_als_per_layer, wandb_grad_als_total, weight_als_per_layer, rel_norms_grads, mode)
        return state, jnp.mean(jnp.array(batch_losses)), avg_bias_al_per_layer, avg_wandb_grad_al_per_layer, avg_wandb_grad_al_total, avg_weight_al_per_layer, avg_rel_norm_grads
    else:
        return state, jnp.mean(jnp.array(batch_losses)), 0, 0, 0, 0, 0


@partial(jax.jit, static_argnums=(3))
def train_step(state, inputs, labels, loss_function):
    """
    Performs a single training step given a batch of data
    ...
    Parameters
    __________
    state : TrainState
        current train state of the model
    inputs : float32
        inputs for the batch
    labels : int32
        labels for the batch
    loss_function: str
        identifier to select loss function
    """
    def loss_fn(params):
        logits = state.apply_fn({'params': params}, inputs)
        loss = get_loss(loss_function, logits, labels)
        return loss
    loss, grads = jax.value_and_grad(loss_fn)(state.params)
    state = state.apply_gradients(grads=grads)
    return state, loss, grads


def get_loss(loss_function, logits, labels):
    """
    Returns the loss for network outputs and labels
    ...
    Parameters
    __________
    loss_function : str
        identifier that slects the correct loss function
    logits : float32
        outputs of the network for the batch
    labels : int32
        labels for the batch
    """
    if (loss_function == "CE"):
        return optax.softmax_cross_entropy_with_integer_labels(logits=jnp.squeeze(logits), labels=labels).mean()
    elif (loss_function == "MSE"):
        return optax.l2_loss(jnp.squeeze(logits), jnp.squeeze(labels)).mean()


def prep_batch(batch):
    """
    Prepares a batch of data for training.
    ...
    Parameters
    __________
    batch : tuple
        batch of data
    seq_len : int
        sequence length used when running model
    in_dim : int
        input dimension of model
    """
    inputs, labels = batch
    inputs = jnp.array(inputs.numpy()).astype(float)
    labels = jnp.array(labels.numpy())
    return inputs, labels


@partial(jnp.vectorize, signature="(c),()->()")
def compute_accuracy(logits, label):
    """
    Computes the accuracy of the network outputs for a batch foe a classification task
    ...
    Parameters
    __________
    logits : float32
        outputs of the network for the batch
    label : int32
        labels for the batch
    """
    return jnp.mean(jnp.argmax(logits) == label)


@partial(jax.jit, static_argnums=(3))
def eval_step(inputs, labels, state, loss_function):
    """
    Performs a single evaluation step given a batch of data
    ...
    Parameters
    __________
    inputs : float32
        inputs for the batch
    labels : int32
        labels for the batch
    state : TrainState
        current train state of the model
    loss_function: str
        identifier to select loss function
    """
    logits = state.apply_fn({"params": state.params}, inputs)
    losses = get_loss(loss_function, logits, labels)
    accs = None
    if loss_function == "CE":
        accs = compute_accuracy((jnp.squeeze(logits)), labels)
    return jnp.mean(losses), accs, logits


def validate(state, testloader, seq_len, in_dim, loss_function):
    """
    Validation function that loops over batches
    ...
    Parameters
    __________
    state : TrainState
        current train state of the model
    testloader : torch.utils.data.DataLoader
        pytorch dataloader for the test set
    seq_len : int
        sequence length used when running model
    in_dim : int
        input dimension of model
    loss_function: str
        identifier to select loss function
    """
    losses, accuracies = jnp.array([]), jnp.array([])

    for batch in tqdm(testloader):
        inputs, labels = prep_batch(batch)
        loss, acc, _ = eval_step(inputs, labels, state, loss_function)
        losses = jnp.append(losses, loss)
        if loss_function == "CE":
            accuracies = jnp.append(accuracies, acc)
        loss_mean = jnp.mean(losses)

    acc_mean = 10000000.
    if loss_function == "CE":
        acc_mean = jnp.mean(accuracies)
    return loss_mean, acc_mean


def pred_step(state, batch, task):
    """
    Prediction function to obtain outputs for a batch of data
    ...
    Parameters
    __________
    state : TrainState
        current train state of the model
    batch : tuple
        batch of data
    task : str 
        identifier for task
    """
    inputs, _ = prep_batch(batch)
    logits = state.apply_fn({'params': state.params}, inputs)
    if (task == "classification"):
        return jnp.squeeze(logits).argmax(axis=1)
    elif (task == "regression"):
        return logits
    else:
        print("Task not supported")
        return None


def plot_sample(testloader, state, seq_len, in_dim, task, output_features):
    """
    Plots a sample of the test set
    ...
    Parameters
    __________
    testloader : torch.utils.data.DataLoader
        pytorch dataloader for the test set
    state : TrainState
        current train state of the model
    seq_len : int
        sequence length used when running model
    in_dim : int
        input dimension of model
    task : str
        identifier for task
    """
    if (task == "classification"):
        return plot_mnist_sample(testloader, state, task)
    elif (task == "regression"):
        return plot_regression_sample(testloader, state, seq_len, in_dim, task, output_features)
    else:
        print("Task not supported")
        return None


def plot_mnist_sample(testloader, state, task):
    """
    Plots a sample of the test set for the MNIST dataset
    ...
    Parameters
    __________
    testloader : torch.utils.data.DataLoader
        pytorch dataloader for the test set
    state : TrainState
        current train state of the model
    task : str
        identifier for task
    """
    test_batch = next(iter(testloader))
    pred = pred_step(state, test_batch, task)

    _, axs = plt.subplots(5, 5, figsize=(12, 12))
    inputs, _ = test_batch
    inputs = torch.reshape(inputs, (inputs.shape[0], 28, 28, 1))
    print(inputs.shape)
    for i, ax in enumerate(axs.flatten()):
        ax.imshow(inputs[i, ..., 0], cmap='gray')
        ax.set_title(f"label={pred[i]}")
        ax.axis('off')
    plt.show()


def plot_regression_sample(testloader, state, seq_len, in_dim, task, output_features):
    """
    Plots a sample of the test set for the regression task
    ...
    Parameters
    __________
    testloader : torch.utils.data.DataLoader
        pytorch dataloader for the test set
    state : TrainState
        current train state of the model
    seq_len : int
        sequence length used when running model
    in_dim : int
        input dimension of model
    task : str
        identifier for task
    """
    inputs_array = np.array([])
    labels_array = np.array([])
    pred_array = np.array([])
    if in_dim != 1 or seq_len != 1 or output_features != 1:
        print("Regression sample plotting only possible for 1D inputs")
        return
    for i in range(5):
        test_batch = next(iter(testloader))
        pred = pred_step(state, test_batch, task)
        inputs, labels = test_batch
        labels = labels
        inputs_array = np.append(inputs_array, inputs)
        labels_array = np.append(labels_array, labels)
        pred_array = np.append(pred_array, pred)
    plt.scatter(inputs_array.flatten(), labels_array.flatten(), label="True")
    plt.scatter(inputs_array.flatten(), pred_array.flatten(), label="Pred")
    plt.show()
