from vunit import VUnit
from vunit.sim_if.hooks import register_hooks

from .library import VCSTLibrary
from ..test.suites import cocotb_run_env
from ..utils.mod_utils import import_mod


class VCST(VUnit):
    def _create_simulator_if(self):
        # A persistent vsim shell would keep the first test's cocotb environment and embedded Python,
        # so every simulation gets its own process. ponytail: also applies to plain VUnit testbenches;
        # use the persistent shell for them if the startup cost matters.
        self._args.unique_sim = True
        simulator_if = super()._create_simulator_if()
        register_hooks(simulator_if.name, run_env=cocotb_run_env)
        return simulator_if

    def library(self, library_name: str):
        """
        Get a library

        :param library_name: The name of the library
        :returns: A :class:`.Library` object
        """
        if not self._project.has_library(library_name):
            raise KeyError(library_name)
        return VCSTLibrary(library_name, self, self._project, self._test_bench_list)

    def add_cocotb_testbench(self, cocotb_module_location, cocotb_module=None):
        """Adds a cocotb module to a library based on variables defined in the cocotb module."""
        top_level = None
        top_level_type = None
        top_level_library = None

        if cocotb_module is None:
            cocotb_module = import_mod(cocotb_module_location)

        if hasattr(cocotb_module, "top_level"):
            top_level = getattr(cocotb_module, "top_level")
        else:
            raise RuntimeError(f"top_level not defined in cocotb module {cocotb_module}")

        if hasattr(cocotb_module, "top_level_type"):
            top_level_type = getattr(cocotb_module, "top_level_type")
        else:
            raise RuntimeError(f"top_level_type not defined in cocotb module {cocotb_module}")

        if hasattr(cocotb_module, "top_level_library"):
            top_level_library = getattr(cocotb_module, "top_level_library")
        else:
            raise RuntimeError(f"top_level_library not defined in cocotb module {cocotb_module}")

        if not isinstance(top_level, str):
            raise RuntimeError(f"top_level defined in cocotb module should be a string {cocotb_module}")

        if not isinstance(top_level_type, str):
            raise RuntimeError(f"top_level_type defined in cocotb module should be a string {cocotb_module}")

        if not isinstance(top_level_library, str):
            raise RuntimeError(f"top_level_library defined in cocotb module should be a string {cocotb_module}")

        if top_level_type not in ("entity", "module"):
            raise RuntimeError(
                f"Invalid top level type '{top_level_type}' in cocotb module {cocotb_module}. "
                f"Please specify either 'entity' or 'module'."
            )

        library = self.library(top_level_library)
        top_level_design_unit = None

        if top_level_type == "entity":
            top_level_design_unit = library.entity(top_level, test_bench=False)
        else:
            top_level_design_unit = library.module(top_level, test_bench=False)

        return library.add_cocotb_testbench(top_level_design_unit, cocotb_module, cocotb_module_location)
