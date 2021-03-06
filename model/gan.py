import numpy as np
import cudarray as ca
import deeppy as dp
import deeppy.expr as expr

from util import ScaleGradient, WeightedParameter


class GAN(dp.base.Model, dp.base.CollectionMixin):
    def __init__(self, n_hidden, generator, discriminator,
                 real_vs_gen_weight=0.5, eps=1e-3):
        self.n_hidden = n_hidden
        self.eps = eps
        self.generator = generator
        self.discriminator = discriminator
        self.real_vs_gen_weight = real_vs_gen_weight
        self.collection = [self.generator, self.discriminator]
        generator.params = [WeightedParameter(p, -1.0, 0.0)
                            for p in generator.params]

    def _generate_expr(self, z):
        return self.generator(z)

    def setup(self, x_shape):
        batch_size = x_shape[0]
        self.x_src = expr.Source(x_shape)
        z = expr.random.normal(size=(batch_size, self.n_hidden))
        x_tilde = self.generator(z)
        x = expr.Concatenate(axis=0)(self.x_src, x_tilde)
        if self.real_vs_gen_weight != 0.5:
            # Scale gradients to balance real vs. generated contributions to
            # GAN discriminator
            dis_batch_size = batch_size*2
            weights = np.zeros((dis_batch_size, 1))
            weights[:batch_size] = self.real_vs_gen_weight
            weights[batch_size:] = (1-self.real_vs_gen_weight)
            dis_weights = ca.array(weights)
            shape = np.array(x_shape)**0
            shape[0] = dis_batch_size
            dis_weights_inv = ca.array(1.0 / np.reshape(weights, shape))
            x = ScaleGradient(dis_weights_inv)(x)
        # Discriminate
        d = self.discriminator(x)
        if self.real_vs_gen_weight != 0.5:
            d = ScaleGradient(dis_weights)(d)
        sign = np.ones((batch_size*2, 1), dtype=ca.float_)
        sign[batch_size:] = -1.0
        offset = np.zeros_like(sign)
        offset[batch_size:] = 1.0
        self.gan_loss = expr.log(d*sign + offset + self.eps)
        self._graph = expr.ExprGraph(-expr.sum(self.gan_loss))
        self._graph.out_grad = ca.array(1.0)
        self._graph.setup()

    @property
    def params(self):
        gen_params = self.generator.params
        dis_params = self.discriminator.params
        return gen_params, dis_params

    def update(self, x):
        self.x_src.out = x
        self._graph.fprop()
        self._graph.bprop()
        d_x_loss = 0
        d_z_loss = 0
        gan_loss = -np.array(self.gan_loss.out)
        batch_size = x.shape[0]
        d_x_loss = float(np.mean(gan_loss[:batch_size]))
        d_z_loss = float(np.mean(gan_loss[batch_size:]))
        return d_x_loss, d_z_loss

    def _batchwise(self, input, expr_fun):
        input = dp.input.Input.from_any(input)
        src = expr.Source(input.x_shape)
        graph = expr.ExprGraph(expr_fun(src))
        graph.setup()
        z = []
        for x_batch in input.batches():
            src.out = x_batch['x']
            graph.fprop()
            z.append(np.array(graph.out))
        z = np.concatenate(z)[:input.n_samples]
        return z

    def decode(self, input):
        """ Hidden to input. """
        return self._batchwise(input, self._generate_expr)


class GradientDescent(dp.GradientDescent):
    def __init__(self, model, input, learn_rule, margin=0.4, equilibrium=0.68):
        super(GradientDescent, self).__init__(model, input, learn_rule)
        self.margin = margin
        self.equilibrium = equilibrium

    def reset(self):
        self.input.reset()
        self.model.setup(**self.input.shapes)
        self.params_gen, self.params_dis = self.model.params

        def states(params):
            return [self.learn_rule.init_state(p) for p in params
                    if not isinstance(p, dp.parameter.SharedParameter)]
        self.lstates_gen = states(self.params_gen)
        self.lstates_dis = states(self.params_dis)

    def train_epoch(self):
        batch_costs = []
        for batch in self.input.batches():
            real_cost, fake_cost = self.model.update(**batch)
            batch_costs.append((real_cost, fake_cost))
            gen_update = True
            dis_update = True
            if self.margin is not None:
                if real_cost < self.equilibrium - self.margin or \
                   fake_cost < self.equilibrium - self.margin:
                    dis_update = False
                if real_cost > self.equilibrium + self.margin or \
                   fake_cost > self.equilibrium + self.margin:
                    gen_update = False
                if not (gen_update or dis_update):
                    gen_update = True
                    dis_update = True
            if gen_update:
                for param, state in zip(self.params_gen, self.lstates_gen):
                    self.learn_rule.step(param, state)
            if dis_update:
                for param, state in zip(self.params_dis, self.lstates_dis):
                    self.learn_rule.step(param, state)
        real_cost = np.mean([cost[0] for cost in batch_costs])
        fake_cost = np.mean([cost[1] for cost in batch_costs])
        print('dis_real:%.4f  dis_fake:%.4f' % (real_cost, fake_cost))
        return real_cost + fake_cost
