import unittest
import os
import fastestimator as fe
from fastestimator.architecture.tensorflow import LeNet
from fastestimator.dataset.data import cifar10
from fastestimator.op.numpyop.univariate import Normalize
from fastestimator.op.tensorop.model import ModelOp
from fastestimator.trace.io import ImageSaver
from fastestimator.trace.xai import Saliency
import tempfile
from fastestimator.test.unittest_util import img_to_rgb_array, check_img_similar


class TestSalinecy(unittest.TestCase):
    def test_saliency(self):
        fe.estimator.enable_deterministic(200)
        label_mapping = {
            'airplane': 0,
            'automobile': 1,
            'bird': 2,
            'cat': 3,
            'deer': 4,
            'dog': 5,
            'frog': 6,
            'horse': 7,
            'ship': 8,
            'truck': 9
        }

        batch_size = 32

        train_data, eval_data = cifar10.load_data()
        pipeline = fe.Pipeline(test_data=train_data,
                               batch_size=batch_size,
                               ops=[Normalize(inputs="x", outputs="x")],
                               num_process=0)

        weight_path = os.path.abspath(os.path.join(__file__, "..", "resources", "lenet_cifar10_tf.h5"))

        model = fe.build(model_fn=lambda: LeNet(input_shape=(32, 32, 3)), optimizer_fn="adam", weights_path=weight_path)
        network = fe.Network(ops=[
            ModelOp(model=model, inputs="x", outputs="y_pred")
        ])

        save_dir = tempfile.mkdtemp()
        traces = [
            Saliency(model=model,
                     model_inputs="x",
                     class_key="y",
                     model_outputs="y_pred",
                     samples=5,
                     label_mapping=label_mapping),
            ImageSaver(inputs="saliency", save_dir=save_dir)
        ]

        estimator = fe.Estimator(pipeline=pipeline, network=network, epochs=5, traces=traces, log_steps=1000)
        estimator.test()

        ans_img_path = os.path.abspath(os.path.join(__file__, "..", "resources", "saliency_figure.png"))
        ans_img = img_to_rgb_array(ans_img_path)
        output_img_path = os.path.join(save_dir, "saliency_test_epoch_5.png")
        output_img = img_to_rgb_array(output_img_path)
        self.assertTrue(check_img_similar(output_img, ans_img))