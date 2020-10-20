# -------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License.txt in the project root for
# license information.
# --------------------------------------------------------------------------

"""
Hummingbird main (converters) API.
"""
from copy import deepcopy
import psutil
import numpy as np

from .operator_converters import constants
from ._parse import parse_sklearn_api_model, parse_onnx_api_model, parse_sparkml_api_model
from ._topology import convert as topology_converter
from ._utils import (
    torch_installed,
    lightgbm_installed,
    xgboost_installed,
    pandas_installed,
    sparkml_installed,
    is_pandas_dataframe,
    is_spark_dataframe,
)
from .exceptions import MissingConverter, MissingBackend
from .supported import backends

# Invoke the registration of all our converters.
from . import operator_converters  # noqa

# Set up the converter dispatcher.
from .supported import xgb_operator_list  # noqa
from .supported import lgbm_operator_list  # noqa


def _is_onnx_model(model):
    """
    Function returning whether the input model is an ONNX model or not.
    """
    return type(model).__name__ == "ModelProto"


def _is_sparkml_model(model):
    """
    Function returning whether the input model is a Spark-ML model or not.
    """
    if sparkml_installed():
        from pyspark.ml import Model, Transformer
        from pyspark.ml.pipeline import PipelineModel

        return isinstance(model, Model) or isinstance(model, PipelineModel) or isinstance(model, Transformer)
    else:
        return False


def _supported_backend_check(backend):
    """
    Function used to check whether the specified backend is supported or not.
    """
    if backend is None:
        raise MissingBackend("Backend: {}".format(backend))


def _supported_backend_check_config(model, backend, extra_config):
    """
    Function used to check whether the specified backend and configuration pair is supported or not.
    """
    assert torch_installed(), "To use Hummingbird you need to install torch."
    import onnx
    import torch

    if backend is torch.jit.__name__ and constants.TEST_INPUT not in extra_config:
        raise RuntimeError("Backend {} requires test inputs. Please pass some test input to the convert.".format(backend))

    if backend is onnx.__name__:
        if constants.ONNX_INITIAL_TYPES not in extra_config and constants.TEST_INPUT not in extra_config:
            raise RuntimeError("Cannot generate test input data for ONNX. Either pass some input data or the initial_types")

    if _is_onnx_model(model) and constants.ONNX_INITIAL_TYPES not in extra_config and constants.TEST_INPUT not in extra_config:
        raise RuntimeError(
            "Cannot extract number of input features from the ONNX. Either pass some input data or the initial_types"
        )


def _convert_sklearn(model, backend, test_input, device, extra_config={}):
    """
    This function converts the specified *scikit-learn* (API) model into its *backend* counterpart.
    The supported operators and backends can be found at `hummingbird.ml.supported`.
    """
    assert model is not None
    assert torch_installed(), "To use Hummingbird you need to install torch."

    # Parse scikit-learn model as our internal data structure (i.e., Topology)
    # We modify the scikit learn model during translation.
    model = deepcopy(model)
    topology = parse_sklearn_api_model(model, extra_config)

    # Convert the Topology object into a PyTorch model.
    hb_model = topology_converter(topology, backend, device, extra_config=extra_config)
    return hb_model


def _convert_lightgbm(model, backend, test_input, device, extra_config={}):
    """
    This function is used to generate a *backend* model from a given input [LightGBM] model.
    [LightGBM]: https://lightgbm.readthedocs.io/
    """
    assert (
        lightgbm_installed()
    ), "To convert LightGBM models you need to install LightGBM (or `pip install hummingbird-ml[extra]`)."

    return _convert_sklearn(model, backend, test_input, device, extra_config)


def _convert_xgboost(model, backend, test_input, device, extra_config={}):
    """
    This function is used to generate a *backend* model from a given input [XGBoost] model.
    [XGBoost]: https://xgboost.readthedocs.io/
    """
    assert (
        xgboost_installed()
    ), "To convert XGboost models you need to instal XGBoost (or `pip install hummingbird-ml[extra]`)."

    # XGBoostRegressor and Classifier have different APIs for extracting the number of features.
    # In the former case we need to infer them from the test_input.
    if "_features_count" in dir(model):
        extra_config[constants.N_FEATURES] = model._features_count
    elif test_input is not None:
        if type(test_input) is np.ndarray and len(test_input.shape) == 2:
            extra_config[constants.N_FEATURES] = test_input.shape[1]
        else:
            raise RuntimeError(
                "XGBoost converter is not able to infer the number of input features.\
                    Please pass a test_input to convert or \
                    fill an issue at https://github.com/microsoft/hummingbird/."
            )
    else:
        raise RuntimeError(
            "XGBoost converter is not able to infer the number of input features.\
                Please pass some test_input to the converter."
        )
    return _convert_sklearn(model, backend, test_input, device, extra_config)


def _convert_onnxml(model, backend, test_input, device, extra_config={}):
    """
    This function converts the specified [ONNX-ML] model into its *backend* counterpart.
    The supported operators can be found at `hummingbird.ml.supported`.
    """
    assert model is not None
    assert torch_installed(), "To use Hummingbird you need to install torch."

    import onnx

    # The conversion requires some test input for tracing.
    # Test inputs can be either provided or generate from the inital types.
    # Get the initial types if any.
    initial_types = None
    if constants.ONNX_INITIAL_TYPES in extra_config:
        initial_types = extra_config[constants.ONNX_INITIAL_TYPES]

    # Generate some test input if necessary.
    if test_input is None:
        if backend == onnx.__name__:
            assert (
                initial_types is not None and not initial_types[0][1].shape is None
            ), "Cannot generate test input data. Initial_types do not contain shape information."
            assert len(initial_types[0][1].shape) == 2, "Hummingbird currently support only inputs with len(shape) == 2."

            from onnxconverter_common.data_types import FloatTensorType, DoubleTensorType, Int32TensorType, Int64TensorType

            test_input = np.random.rand(initial_types[0][1].shape[0], initial_types[0][1].shape[1])
            if type(initial_types[0][1]) is FloatTensorType:
                test_input = np.array(test_input, dtype=np.float32)
            elif type(initial_types[0][1]) is DoubleTensorType:
                test_input = np.array(test_input, dtype=np.float64)
            elif type(initial_types[0][1]) is Int32TensorType:
                test_input = np.array(test_input, dtype=np.int32)
            elif type(initial_types[0][1]) is Int64TensorType:
                test_input = np.array(test_input, dtype=np.int64)
            else:
                raise RuntimeError(
                    "Type {} not supported. Please fill an issue on https://github.com/microsoft/hummingbird/.".format(
                        type(initial_types[0][1])
                    )
                )
            extra_config[constants.TEST_INPUT] = test_input

    # Set the number of expected input features.
    if constants.N_FEATURES not in extra_config:
        extra_config[constants.N_FEATURES] = test_input.shape[1]

    # Set the initializers. Some converter requires the access to initializers.
    initializers = {} if model.graph.initializer is None else {in_.name: in_ for in_ in model.graph.initializer}
    extra_config[constants.ONNX_INITIALIZERS] = initializers

    # Parse ONNX model as our internal data structure (i.e., Topology).
    topology = parse_onnx_api_model(model)

    # Convert the Topology object into a PyTorch model.
    hb_model = topology_converter(topology, backend, device, extra_config=extra_config)
    return hb_model


def _convert_sparkml(model, backend, test_input, device, extra_config={}):
    """
    This function converts the specified *Spark-ML* (API) model into its *backend* counterpart.
    The supported operators and backends can be found at `hummingbird.ml.supported`.
    """
    assert model is not None
    assert torch_installed(), "To use Hummingbird you need to install torch."

    # Parse Spark-ML model as our internal data structure (i.e., Topology)
    # We modify the Spark-ML model during translation.
    model = model.copy()
    topology = parse_sparkml_api_model(model, extra_config)

    # Convert the Topology object into a PyTorch model.
    hb_model = topology_converter(topology, backend, device, extra_config=extra_config)
    return hb_model


def convert(model, backend, test_input=None, device="cpu", extra_config={}):
    """
    This function converts the specified input *model* into an implementation targeting *backend*.
    *Convert* supports [Sklearn], [LightGBM], [XGBoost], [ONNX], and [SparkML] models.
    For *LightGBM* and *XGBoost* currently only the Sklearn API is supported.
    The detailed list of models and backends can be found at `hummingbird.ml.supported`.
    The *onnx* backend requires either a test_input of a the initial types set through the exta_config parameter.
    The *torch.jit* backend requires a test_input.
    [Sklearn]: https://scikit-learn.org/
    [LightGBM]: https://lightgbm.readthedocs.io/
    [XGBoost]: https://xgboost.readthedocs.io/
    [ONNX]: https://onnx.ai/
    [ONNX-ML]: https://github.com/onnx/onnx/blob/master/docs/Operators-ml.md
    [ONNX operators]: https://github.com/onnx/onnx/blob/master/docs/Operators.md
    [Spark-ML]: https://spark.apache.org/docs/latest/api/python/pyspark.ml.html

    Args:
        model: An input model
        backend: The target for the conversion
        test_input: Some input data used to trace the model execution.
                    Multiple inputs can be passed as `tuple` objects or pandas Dataframes.
                    When possible, (`numpy`)`arrays` are suggesed.
        device: The target device the model should be run. This parameter is only used by the *torch** backends, and
                the devices supported are the one supported by PyTorch, i.e., 'cpu' or 'cuda'.
        extra_config: Extra configurations to be used by the individual operator converters.
                      The set of supported extra configurations can be found at `hummingbird.ml.supported`

    Examples:
        >>> pytorch_model = convert(sklearn_model,`torch`)

    Returns:
        A model implemented in *backend*, which is equivalent to the input model
    """
    assert model is not None

    # We destroy extra_config during conversion, we create a copy here.
    extra_config = deepcopy(extra_config)

    # Set some default configurations.
    # Add test input as extra configuration for conversion.
    if (
        test_input is not None
        and constants.TEST_INPUT not in extra_config
        and (is_spark_dataframe(test_input) or len(test_input) > 0)
    ):
        extra_config[constants.TEST_INPUT] = test_input
    # By default we return the converted model wrapped into a `hummingbird.ml._container.SklearnContainer` object.
    if constants.CONTAINER not in extra_config:
        extra_config[constants.CONTAINER] = True
    # By default we set num of intra-op parallelism to be the number of physical cores available
    if constants.N_THREADS not in extra_config:
        extra_config[constants.N_THREADS] = psutil.cpu_count(logical=False)

    # Fix the test_input type
    if constants.TEST_INPUT in extra_config:
        if type(extra_config[constants.TEST_INPUT]) == list:
            extra_config[constants.TEST_INPUT] = np.array(extra_config[constants.TEST_INPUT])
        elif type(extra_config[constants.TEST_INPUT]) == tuple:
            # We are passing multiple datasets.
            assert all(
                [type(input) == np.ndarray for input in extra_config[constants.TEST_INPUT]]
            ), "When passing multiple inputs only ndarrays are supported."
            assert all([len(input.shape) == 2 for input in extra_config[constants.TEST_INPUT]])
            extra_config[constants.N_FEATURES] = sum([input.shape[1] for input in extra_config[constants.TEST_INPUT]])
            extra_config[constants.N_INPUTS] = len(extra_config[constants.TEST_INPUT])
        elif pandas_installed() and is_pandas_dataframe(extra_config[constants.TEST_INPUT]):
            # We split the input dataframe into columnar ndarrays
            extra_config[constants.N_INPUTS] = len(extra_config[constants.TEST_INPUT].columns)
            extra_config[constants.N_FEATURES] = extra_config[constants.N_INPUTS]
            input_names = list(extra_config[constants.TEST_INPUT].columns)
            splits = [extra_config[constants.TEST_INPUT][input_names[idx]] for idx in range(extra_config[constants.N_INPUTS])]
            splits = [df.to_numpy().reshape(-1, 1) for df in splits]
            extra_config[constants.TEST_INPUT] = tuple(splits) if len(splits) > 1 else splits[0]
            extra_config[constants.INPUT_NAMES] = input_names
        elif sparkml_installed() and is_spark_dataframe(extra_config[constants.TEST_INPUT]):
            from pyspark.ml.linalg import DenseVector, SparseVector, VectorUDT
            from pyspark.sql.types import ArrayType, FloatType, DoubleType, IntegerType, LongType

            df = extra_config[constants.TEST_INPUT]
            input_names = [field.name for field in df.schema.fields]
            extra_config[constants.N_INPUTS] = len(input_names)
            extra_config[constants.N_FEATURES] = extra_config[constants.N_INPUTS]

            size = df.count()
            row_dict = df.take(1)[0].asDict()
            splits = []
            for field in df.schema.fields:
                data_col = row_dict[field.name]
                spark_dtype = type(field.dataType)
                shape = 1
                if spark_dtype in [DenseVector, VectorUDT]:
                    np_dtype = np.float64
                    shape = data_col.array.shape[0]
                elif spark_dtype == SparseVector:
                    np_dtype = np.float64
                    shape = data_col.size
                elif spark_dtype == ArrayType:
                    np_dtype = np.float64
                    shape = len(data_col)
                elif spark_dtype == IntegerType:
                    np_dtype = np.int32
                elif spark_dtype == FloatType:
                    np_dtype = np.float32
                elif spark_dtype == DoubleType:
                    np_dtype = np.float64
                elif spark_dtype == LongType:
                    np_dtype = np.int64
                else:
                    raise ValueError("Unrecognized data type: {}".format(spark_dtype))

                splits.append(np.zeros((size, shape), np_dtype))

            extra_config[constants.TEST_INPUT] = tuple(splits) if len(splits) > 1 else splits[0]
            extra_config[constants.INPUT_NAMES] = input_names

        test_input = extra_config[constants.TEST_INPUT]

    # We do some normalization on backends.
    backend = backend.lower()
    backend = backends[backend]

    # Check whether we actually support the backend.
    _supported_backend_check(backend)
    _supported_backend_check_config(model, backend, extra_config)

    if type(model) in xgb_operator_list:
        return _convert_xgboost(model, backend, test_input, device, extra_config)

    if type(model) in lgbm_operator_list:
        return _convert_lightgbm(model, backend, test_input, device, extra_config)

    if _is_onnx_model(model):
        return _convert_onnxml(model, backend, test_input, device, extra_config)

    if _is_sparkml_model(model):
        return _convert_sparkml(model, backend, test_input, device, extra_config)

    return _convert_sklearn(model, backend, test_input, device, extra_config)
