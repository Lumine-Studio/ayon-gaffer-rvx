import attr
import Gaffer
from openpype.hosts.gaffer.api.lib import get_color_management_preferences
from openpype.pipeline.colorspace import get_display_view_colorspace_name


@attr.s
class LayerMetadata(object):
    """Data class for Render Layer metadata."""
    pass


@attr.s
class RenderProduct(object):
    """Getting Colorspace as
    Specific Render Product Parameter for submitting
    publish job.

    """
    colorspace = attr.ib()                      # colorspace
    view = attr.ib()
    productName = attr.ib(default=None)


class ARenderProduct(object):

    def __init__(self, script_node):
        """Constructor."""
        # Initialize
        self.script_node = script_node
        self.layer_data = self._get_layer_data()
        self.layer_data.products = self.get_colorspace_data()

    def _get_layer_data(self):
        return LayerMetadata(
        )

    def get_colorspace_data(self):
        """To be implemented by renderer class.

        This should return a list of RenderProducts.

        Returns:
            list: List of RenderProduct

        """
        data = get_color_management_preferences(self.script_node)
        colorspace_data = [
            RenderProduct(
                colorspace=data["display"],
                view=data["view"],
                productName=""
            )
        ]
        return colorspace_data
