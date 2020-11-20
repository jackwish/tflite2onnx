import copy
import logging
import tflite
import numpy as np

from tflite2onnx import mapping
from tflite2onnx.layout import Layout
from tflite2onnx.op.common import Operator
from tflite2onnx.op.transpose import Transpose

logger = logging.getLogger('tflite2onnx')


class Reshape(Operator):
    TypeMapping = {
        tflite.BuiltinOperator.RESHAPE: 'Reshape',
    }

    def __init__(self, TFactory, index):
        super().__init__(TFactory, index)

        self.forFakeBroadcasting = False

        self.setInited()

    @property
    def type(self):
        return 'Reshape'

    def parse(self):
        logger.debug("Parsing %s...", self.type)

        op = self.tflite
        opcode = self.model.OperatorCodes(op.OpcodeIndex()).BuiltinCode()
        assert(opcode is tflite.BuiltinOperator.RESHAPE)

        assert(op.InputsLength() >= 1)
        assert(op.OutputsLength() == 1)

        # input
        self.parseInput(0)

        # This block has been commented
        # because the `Reshape` with only one input seems like a special case
        # haven't manage to reproduce currently
        # if op.InputsLength() == 1:
        #     # options
        #     op_opt = op.BuiltinOptions()
        #     option = tflite.ReshapeOptions()
        #     option.Init(op_opt.Bytes, op_opt.Pos)
        #     sp = option.NewShapeAsNumpy()
        #     sp = self.TFactory.createVector(sp.astype('int64'))
        #     sp.addConsumer(self)
        #     self.inputs.append(sp)

        if op.InputsLength() == 2:
            # shape
            st = self.parseInput(1)

            # TFLite shape is int32 data type, ONNX is int64
            st.dtype = mapping.DTYPE_NAME2ONNX['int64']
            if st.isInitializer:
                st.data = st.data.astype('int64')
            if len(st.shape) > 1:
                logger.warning("ONNXRuntime doesn't support 2+rank shape, "
                               "flatten if the shape is initialzier, ignore otherwise."
                               "https://github.com/jackwish/tflite2onnx/issues/16")
                if st.isInitializer:
                    st.shape = (np.prod(np.array(st.shape)),)

        # output
        self.parseOutput(0)

        self.setParsed()

    def preserveInputSpatialSemantic(self):
        # If the input (or output) tensor of Reshape
        # holds a special data layout semantic,
        # we need to insert Transpose before (or after) the Reshape operator
        # https://github.com/jackwish/tflite2onnx/issues/28
        # An example for inserting `Transpose` before `Reshape`
        #    ------
        #   | Conv |
        #    ------
        #      |
        #      |  to_transpose (Original input of `Reshape`)
        #      |  (e.g. NCHW)
        #   -------
        #  | Trans |  e.g. perm: (0, 2, 3, 1)
        #   -------
        #      |
        #      |  transposed (New created tensor)
        #      |  (e.g. NHWC)
        #   --------
        #  | Reshape |
        #   --------
        #

        assert(self.status.parsed)
        to_transpose = self.inputs[0]

        transposed_name = 'TFLITE2ONNX_Transposed_%s' % to_transpose.name
        transposed = self.TFactory.getWithRef(to_transpose, transposed_name, True)

        # Construct the layout as original input of `Reshape`
        # e.g. source: NHWC, target: NCHW
        # Exchange the source and target layout for layout preservation
        layout = Layout(to_transpose.layout.target, to_transpose.layout.source)
        transposed.shape = layout.transform(to_transpose.shape)
        transposed.setParsed()

        # Construct the additional transpose before `Reshape`
        trans = Transpose(self.TFactory, -1)
        # e.g. From NCHW to NHWC
        trans.attrs['perm'] = layout.perm

        trans.inputs.append(to_transpose)
        to_transpose.replaceConsumer(self, trans)
        self.replaceInput(to_transpose, transposed)

        trans.outputs.append(transposed)
        transposed.addProducer(trans)
        transposed.addConsumer(self)
        trans.setParsed()

        self.pre.append(trans)

    def preserveOutputSpatialSemantic(self):
        # If the input (or output) tensor of Reshape
        # holds a special data layout semantic,
        # we need to insert Transpose before (or after) the Reshape operator
        # https://github.com/jackwish/tflite2onnx/issues/28

        # An example for inserting `Transpose` after `Reshape`
        #   -------
        # | Reshape |
        #   -------
        #      |
        #      |  to_transpose (New created tensor)
        #      |  (e.g. NHWC)
        #   -------
        #  | Trans |  e.g. perm: (0, 3, 1, 2)
        #   -------
        #      |
        #      |  transposed (Original `Reshape` output)
        #      |  (e.g. NCHW)
        #    ------
        #   | Conv |
        #    ------
        #

        assert(self.status.parsed)
        transposed = self.outputs[0]

        to_transpose_name = 'TFLITE2ONNX_ToTranspose_%s' % transposed.name
        to_transpose = self.TFactory.getWithRef(transposed, to_transpose_name, True)

        # Construct a layout as original output of `Reshape`
        # e.g. source: NCHW, target: NHWC
        # Exchange the source and target layout for layout preservation
        layout = Layout(transposed.layout.target, transposed.layout.source)
        to_transpose.shape = layout.transform(transposed.shape)
        to_transpose.setParsed()

        # Construct the additional transpose after `Reshape`
        trans = Transpose(self.TFactory, -1)
        # e.g. From NHWC to NCHW
        trans.attrs['perm'] = transposed.layout.perm

        trans.inputs.append(to_transpose)
        transposed.replaceProducer(self, trans)
        self.replaceOutput(transposed, to_transpose)

        trans.outputs.append(transposed)
        to_transpose.addProducer(self)
        to_transpose.addConsumer(trans)
        trans.setParsed()
        # Rename the new `Transpose` operator
        # to avoid the same name with 'Reshape'
        trans.name = 'TFLITE2ONNX_Transpose_%s' % transposed.name

        self.post.append(trans)

    def propagatableTensors(self):
        return list()

    def transform(self):
        i = self.inputs[0]
        o = self.outputs[0]

        if self.forFakeBroadcasting:
            assert(len(i.shape) != len(o.shape))
            shape_t = self.inputs[1]
            layout = copy.deepcopy(o.layout)
            if layout is None:
                raise ValueError("Requires layout description for <%s>" % i.name)
            shape_t.data = layout.transform(shape_t.data)
        else:
            # Insert a `Transpose` before/after `Reshape`
            # if a explicit layout is required
            if i.layout:
                self.preserveInputSpatialSemantic()
            if o.layout:
                self.preserveOutputSpatialSemantic()
