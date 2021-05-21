# Copyright 2021 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Implementation of proximal gradient descent in JAX."""

from typing import Any
from typing import Callable
from typing import Optional
from typing import Union

import jax
import jax.numpy as jnp

from jaxopt import base
from jaxopt import implicit_diff as idf
from jaxopt import linear_solve
from jaxopt import loop
from jaxopt.tree_util import tree_add_scalar_mul
from jaxopt.tree_util import tree_l2_norm
from jaxopt.tree_util import tree_sub
from jaxopt.tree_util import tree_vdot


def _make_prox_grad(prox, params_prox):
  """Makes the update function:

    prox(curr_x - curr_stepsize * curr_x_fun_grad, params_prox, curr_stepsize)
  """

  def prox_grad(curr_x, curr_x_fun_grad, curr_stepsize):
    update = tree_add_scalar_mul(curr_x, -curr_stepsize, curr_x_fun_grad)
    return prox(update, params_prox, curr_stepsize)

  return prox_grad


def _make_linesearch(fun, params_fun, prox_grad, maxls, stepfactor, unroll):
  """Makes the backtracking line search."""

  def linesearch(curr_x, curr_x_fun_val, curr_x_fun_grad, curr_stepsize):
    # epsilon of current dtype for robust checking of
    # sufficient decrease condition
    eps = jnp.finfo(curr_x_fun_val.dtype).eps

    def cond_fun(args):
      next_x, stepsize = args
      diff_x = tree_sub(next_x, curr_x)
      sqdist = tree_l2_norm(diff_x, squared=True)
      # The expression below checks the sufficient decrease condition
      # f(next_x) < f(x) + dot(grad_f(x), diff_x) + (0.5/stepsize) ||diff_x||^2
      # where the terms have been reordered for numerical stability.
      fun_decrease = stepsize * (fun(next_x, params_fun) - curr_x_fun_val)
      condition = stepsize * tree_vdot(diff_x, curr_x_fun_grad) + 0.5 * sqdist
      return fun_decrease > condition + eps

    def body_fun(args):
      stepsize = args[1]
      next_stepsize = stepsize * stepfactor
      next_x = prox_grad(curr_x, curr_x_fun_grad, next_stepsize)
      return next_x, next_stepsize

    init_x = prox_grad(curr_x, curr_x_fun_grad, curr_stepsize)
    init_val = (init_x, curr_stepsize)

    return loop.while_loop(
        cond_fun=cond_fun,
        body_fun=body_fun,
        init_val=init_val,
        maxiter=maxls,
        unroll=unroll,
        jit=True)

  return linesearch


def _make_pg_body_fun(fun: Callable,
                      params_fun: Optional[Any] = None,
                      prox: Optional[Callable] = None,
                      params_prox: Optional[Any] = None,
                      stepsize: float = 0.0,
                      maxls: int = 15,
                      acceleration: bool = True,
                      unroll_ls: bool = False,
                      stepfactor: float = 0.5) -> Callable:
  """Creates a body_fun for performing one iteration of proximal gradient."""

  fun = jax.jit(fun)
  value_and_grad_fun = jax.jit(jax.value_and_grad(fun))
  grad_fun = jax.jit(jax.grad(fun))
  prox_grad = _make_prox_grad(prox, params_prox)
  linesearch = _make_linesearch(
      fun=fun,
      params_fun=params_fun,
      prox_grad=prox_grad,
      maxls=maxls,
      stepfactor=stepfactor,
      unroll=unroll_ls)

  def error_fun(curr_x, curr_x_fun_grad):
    next_x = prox_grad(curr_x, curr_x_fun_grad, 1.0)
    diff_x = tree_sub(next_x, curr_x)
    return tree_l2_norm(diff_x)

  def _iter(curr_x, curr_x_fun_val, curr_x_fun_grad, curr_stepsize):
    if stepsize <= 0:
      # With line search.
      next_x, next_stepsize = linesearch(curr_x, curr_x_fun_val,
                                         curr_x_fun_grad, curr_stepsize)

      # If step size becomes too small, we restart it to 1.0.
      # Otherwise, we attempt to increase it.
      next_stepsize = jnp.where(next_stepsize <= 1e-6, 1.0,
                                next_stepsize / stepfactor)

      return next_x, next_stepsize
    else:
      # Without line search.
      next_x = prox_grad(curr_x, curr_x_fun_grad, stepsize)
      return next_x, stepsize

  def body_fun_proximal_gradient(args):
    iter_num, curr_x, curr_stepsize, _ = args
    curr_x_fun_val, curr_x_fun_grad = value_and_grad_fun(curr_x, params_fun)
    next_x, next_stepsize = _iter(curr_x, curr_x_fun_val, curr_x_fun_grad,
                                  curr_stepsize)
    curr_error = error_fun(curr_x, curr_x_fun_grad)
    return iter_num + 1, next_x, next_stepsize, curr_error

  def body_fun_accelerated_proximal_gradient(args):
    iter_num, curr_x, curr_y, curr_t, curr_stepsize, _ = args
    curr_y_fun_val, curr_y_fun_grad = value_and_grad_fun(curr_y, params_fun)
    next_x, next_stepsize = _iter(curr_y, curr_y_fun_val, curr_y_fun_grad,
                                  curr_stepsize)
    next_t = 0.5 * (1 + jnp.sqrt(1 + 4 * curr_t**2))
    diff_x = tree_sub(next_x, curr_x)
    next_y = tree_add_scalar_mul(next_x, (curr_t - 1) / next_t, diff_x)
    next_x_fun_grad = grad_fun(next_x, params_fun)
    next_error = error_fun(next_x, next_x_fun_grad)
    return iter_num + 1, next_x, next_y, next_t, next_stepsize, next_error

  if acceleration:
    return body_fun_accelerated_proximal_gradient
  else:
    return body_fun_proximal_gradient


def _proximal_gradient(fun, init, params_fun, prox, params_prox, stepsize,
                       maxiter, maxls, tol, acceleration, verbose,
                       implicit_diff, ret_info):

  def cond_fun(args):
    iter_num = args[0]
    error = args[-1]
    if verbose:
      print(iter_num, error)
    return error > tol

  body_fun = _make_pg_body_fun(
      fun=fun,
      params_fun=params_fun,
      prox=prox,
      params_prox=params_prox,
      stepsize=stepsize,
      maxls=maxls,
      acceleration=acceleration,
      unroll_ls=not implicit_diff)

  if acceleration:
    # iter_num, curr_x, curr_y, curr_t, curr_stepsize, error
    args = (0, init, init, 1.0, 1.0, jnp.inf)
  else:
    # iter_num, curr_x, curr_stepsize, error
    args = (0, init, 1.0, jnp.inf)

  # We always jit unless verbose mode is enabled.
  jit = not verbose
  # We unroll when implicit diff is disabled or when jit is disabled.
  unroll = not implicit_diff or not jit

  res = loop.while_loop(
      cond_fun=cond_fun,
      body_fun=body_fun,
      init_val=args,
      maxiter=maxiter,
      unroll=unroll,
      jit=jit)

  if ret_info:
    return base.OptimizeResults(x=res[1], nit=res[0], error=res[-1])
  else:
    return res[1]


def make_solver_fun(fun: Callable,
                    prox: Callable,
                    init: Any,
                    stepsize: float = 0.0,
                    maxiter: int = 500,
                    maxls: int = 15,
                    tol: float = 1e-3,
                    acceleration: bool = True,
                    verbose: int = 0,
                    implicit_diff: Union[bool, Callable] = True,
                    ret_info: bool = False,
                    has_aux: bool = False) -> Callable:
  """Creates a proximal gradient (a.k.a. FISTA) solver function
  ``solver_fun(params_fun, params_prox)`` for solving::

    argmin_x fun(x, params_fun) + g(x, params_prox),

  where fun is smooth and g is possibly non-smooth. This method is a specific
  instance of (accelerated) projected gradient descent when the prox is a
  projection and (acclerated) gradient descent when prox is ``prox_none``.

  The stopping criterion is::

    ||x - prox(x - grad(fun)(x, params_fun), params_prox)||_2 <= tol.

  Args:
    fun: a smooth function of the form ``fun(x, params_fun)``.
    prox: proximity operator associated with the function g.
    init: initialization to use for x (pytree).
    stepsize: a stepsize to use (if <= 0, use backtracking line search).
    maxiter: maximum number of proximal gradient descent iterations.
    maxls: maximum number of iterations to use in the line search.
    tol: tolerance to use.
    acceleration: whether to use acceleration (also known as FISTA) or not.
    verbose: whether to print error on every iteration or not. verbose=True will
      automatically disable jit.
    implicit_diff: if True, enable implicit differentiation using cg,
      if Callable, do implicit differentiation using callable as linear solver,
      if False, use autodiff through the solver implementation (note:
        this will unroll syntactic loops).
    ret_info: whether to return an OptimizeResults object containing additional
      information regarding the solution
    has_aux: whether function fun outputs one (False) or more values (True).
      When True it will be assumed by default that fun(...)[0] is the objective.


  Returns:
    Solver function ``solver_fun(params_fun, params_prox)``.

  References:
    Beck, Amir, and Marc Teboulle. "A fast iterative shrinkage-thresholding
    algorithm for linear inverse problems." SIAM imaging sciences (2009)

    Nesterov, Yu. "Gradient methods for minimizing composite functions."
    Mathematical Programming (2013).
  """
  _fun = fun if not has_aux else lambda x, par: fun(x, par)[0]
  def solver_fun(params_fun=None, params_prox=None):
    return _proximal_gradient(_fun, init, params_fun, prox, params_prox,
                              stepsize, maxiter, maxls, tol, acceleration,
                              verbose, implicit_diff, ret_info)

  if implicit_diff:
    if isinstance(implicit_diff, Callable):
      solve = implicit_diff
    else:
      solve = linear_solve.solve_normal_cg
    fixed_point_fun = idf.make_proximal_gradient_fixed_point_fun(_fun, prox)
    solver_fun = idf.custom_fixed_point(fixed_point_fun,
                                        unpack_params=True,
                                        solve=solve)(solver_fun)
  return solver_fun