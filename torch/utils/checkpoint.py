import torch
from torch.autograd import Variable, Function


def repackage_inputs(inputs):
    if torch.is_tensor(inputs):
        return Variable(inputs)
    elif isinstance(inputs, tuple):
        return tuple(repackage_inputs(v) for v in inputs)
    else:
        raise RuntimeError("Unknown input type")


def unpack_variables(inputs):
    if type(inputs) == Variable:
        return inputs.data
    elif torch.is_tensor(inputs):
        return inputs
    elif isinstance(inputs, tuple):
        return tuple(unpack_variables(v) for v in inputs)
    else:
        raise RuntimeError("Unknown input type")


class CheckpointFunction(Function):

    # NOTE: *args is the flat inputs list, args is the tuple containing inputs
    @staticmethod
    def forward(ctx, run_function, *args):
        ctx.run_function = run_function
        ctx.save_for_backward(*args)
        inputs = repackage_inputs(args)
        with torch.no_grad():
            outputs = run_function(*inputs)     # the *inputs* is always a tuple
        return unpack_variables(outputs)

    @staticmethod
    def backward(ctx, *args):
        inputs = ctx.saved_variables
        with torch.enable_grad():
            outputs = ctx.run_function(*inputs)

        if isinstance(outputs, tuple):
            output_list = list(outputs)
        elif isinstance(outputs, Variable) or torch.is_tensor(outputs):
            output_list = [outputs]
        grad_outputs = [grad for grad in args]

        # some inputs might not need gradients so we filter them out
        # and later return None as grad for those inputs
        filtered_inputs = []
        for i in range(len(inputs)):
            if inputs[i].requires_grad:
                filtered_inputs.append(inputs[i])
        assert len(filtered_inputs) > 0, "No input requires gradients"

        torch.autograd.grad(output_list, filtered_inputs, grad_outputs)

        # append None for input grads which don't require grad. The first input
        # is a run_function whose grad is None
        input_grads = [None]
        for i in range(len(inputs)):
            if inputs[i].requires_grad:
                input_grads.append(inputs[i].grad)
            else:
                input_grads.append(None)
        return tuple(input_grads)


def checkpoint(run_function, *args):
    r"""Checkpoint a model or part of the model

    Checkpoint works by trading compute for memory. It can be applied on any
    part of the model. In the forward pass, the model is run in volatile
    manner i.e. the activations are not stored. The forward pass save the
    inputs tuple and the run_function parameter. In the backwards pass, the
    saved inputs and run_function is retreived, and the forward pass is done
    on the model again (non-volatile this time) since we need to get the
    activations values for calculating the gradient and then the gradients are
    calculated.

    Args:
        run_function : describes what to run in the forward pass of the model or
                       part of the model. It should also know how to handle
                       the inputs passed as the tuple. For example, in LSTM,
                       user passes (activation, hidden), run_function should
                       correctly use first input as activation and second input
                       as hidden
        args:         tuple containing inputs to the run_function

    Returns:
        Output of running the run_function on *args
    """
    return CheckpointFunction.apply(run_function, *args)


def checkpoint_sequential(modules, segments, *inputs):
    r"""A helper function for checkpointing sequential based models.

    For models that are constructed using sequential, they normally are built
    using various modules. For such models, given a list of modules it executes
    sequentially, we can divide the model in various segments and checkpoint
    the segments. All segments except the last will be run in volatile manner.
    The inputs of each checkpointed segment will be saved for re-running the
    segment in the backward pass.

    Args:
        modules: The sequence of modules (comprising the model) to run in order.
                 Usually
                    modules = [module for k, module in self._modules.items()][0]

        segments: Number of times chunks to create in the model

        inputs: tuple containing the inputs to run_function

    Returns:
        Output of running the modules on *inputs

    Example:
        >>> modules = [module for k, module in self._modules.items()][0]
        >>> input_var = Variable(x.data, requires_grad=True)
        >>> input_var = checkpoint_sequential(modules, chunks, input_var)
    """

    def run_function(start, end, modules):
        def forward(*inputs):
            input = inputs[0]
            for j in range(start, end + 1):
                input = modules[j](input)
            return input
        return forward

    segment_size = len(modules) // segments
    # the last chunk has to be non-volatile
    for start in range(0, segment_size * (segments - 1), segment_size):
        end = start + segment_size - 1
        inputs = checkpoint(run_function(start, end, modules), *inputs)
        if not isinstance(inputs, tuple):
            inputs = (inputs,)
    return run_function(end + 1, len(modules) - 1, modules)(*inputs)
