import os
import re
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
import xml.etree.ElementTree as ET

import find_libpython
from cocotb_tools.config import lib_name_path, pygpi_entry_point

from vunit.test.report import PASSED, SKIPPED, FAILED
from vunit.ostools import write_file
from vunit.test.suites import IndependentSimTestCase, TestRun, get_result_file_name as get_vunit_result_file_name

# VUnit's run_env hooks are registered per simulator, not per test, and tests run in threads.
# The cocotb environment of the test running in the current thread is kept here for the hook.
_thread_local = threading.local()


def cocotb_run_env(simulator_interface, env):
    """VUnit run_env hook merging the current test's cocotb environment into the simulator's."""
    return {**env, **(getattr(_thread_local, "env", None) or {})}


@contextmanager
def simulation_env(env):
    """Pass env to every simulator process started by this thread inside the with block."""
    _thread_local.env = env
    try:
        yield
    finally:
        _thread_local.env = None


class IndependentCocoSimTestCase(IndependentSimTestCase):
    def __init__(self, test, config, simulator_if, seed, *, elaborate_only=False):
        self._name = self.get_name(test, config)
        self._configuration = config
        self._test = test

        self._run = CocoTestRun(
            vhdl=test._vhdl,
            top_level=test._top_level,
            simulator_if=simulator_if,
            config=config,
            elaborate_only=elaborate_only,
            cocotb_module_location=test._cocotb_module_location,
            test_suite_name=self._name,
            test_cases=[test.name],
            seed=seed,
        )


class CocoTestRun(TestRun):
    def __init__(self, *, vhdl, top_level, cocotb_module_location, **kwargs):
        TestRun.__init__(self, **kwargs)
        self._cocotb_module_location = cocotb_module_location
        self._top_level = top_level
        self._vhdl = vhdl

    def create_environ(self, output_path):
        """Environment variables cocotb needs inside the simulator process."""
        mod_dir, mod_name = os.path.split(self._cocotb_module_location)
        python_path = os.environ.get("PYTHONPATH")
        test_filter = "|".join(re.escape(f"{mod_name}.{name}") for name in self._test_cases)

        environ = {
            "PYTHONPATH": str(mod_dir) if not python_path else f"{python_path}{os.pathsep}{mod_dir}",
            "PYGPI_PYTHON_BIN": sys.executable,
            "COCOTB_RESULTS_FILE": get_result_file_name(output_path),
            "COCOTB_TEST_MODULES": mod_name,
            "COCOTB_TEST_FILTER": f"^({test_filter})$",
            "COCOTB_TOPLEVEL": self._top_level.lower() if self._vhdl else self._top_level,
            "TOPLEVEL_LANG": "vhdl" if self._vhdl else "verilog",
        }

        if "GPI_USERS" not in os.environ:
            libpython = os.environ.get("LIBPYTHON_LOC") or find_libpython.find_libpython()
            environ["GPI_USERS"] = f"{libpython};{pygpi_entry_point()}"

        return environ

    def configure_simulator(self, config):
        """Add the simulator flags that load cocotb's VPI/VHPI/FLI library."""
        for name, flags in get_sim_flags(self._simulator_if.name, self._vhdl).items():
            config.set_sim_option(name, config.sim_options.get(name, []) + flags)
            # vsim_flags.gui replaces vsim_flags in GUI mode when set, so it needs the cocotb flags too.
            if f"{name}.gui" in config.sim_options:
                config.set_sim_option(f"{name}.gui", config.sim_options[f"{name}.gui"] + flags)

    def run(self, output_path, read_output, run_script_path=None):
        """
        Run selected test cases within the test suite

        Returns a dictionary of test results
        """
        results = {name: FAILED for name in self._test_cases}

        if not self._config.call_pre_config(output_path, self._simulator_if.output_path, self.get_seed()):
            return results

        # VUnit's vsim run script reads its own results file, as for a VUnit testbench
        write_file(get_vunit_result_file_name(output_path), "")

        sim_ok = self._simulate(output_path)

        if self._elaborate_only:
            status = PASSED if sim_ok else FAILED
            return dict((name, status) for name in self._test_cases)

        results = self._read_test_results(file_name=get_result_file_name(output_path))

        done, results = self._check_results(results, sim_ok)
        if done:
            return results

        if not self._config.call_post_check(output_path, read_output):
            for name in self._test_cases:
                results[name] = FAILED

        return results

    def _simulate(self, output_path):
        config = self._config.copy()
        self.configure_simulator(config)
        with simulation_env(self.create_environ(output_path)):
            return self._simulator_if.simulate(
                output_path=output_path,
                test_suite_name=self._test_suite_name,
                config=config,
                elaborate_only=self._elaborate_only,
            )

    def _read_test_results(self, file_name):
        results = {name: FAILED for name in self._test_cases}

        if not os.path.exists(file_name):
            return results

        results_tests = []
        for result in ET.parse(file_name).getroot().iter("testcase"):
            test_name = result.attrib["name"]
            results_tests.append(test_name)
            if result.find("skipped") is not None:
                results[test_name] = SKIPPED
            elif result.find("failure") is not None or result.find("error") is not None:
                results[test_name] = FAILED
            else:
                results[test_name] = PASSED

        for test_name in self._test_cases:
            if test_name not in results_tests:
                results[test_name] = SKIPPED

        return results


def get_sim_flags(simulator_name, vhdl):
    """Simulator options that load cocotb into the given VUnit simulator."""
    if simulator_name == "ghdl":
        return {"ghdl.sim_flags": [f"--vpi={lib_name_path('vpi', 'ghdl')}"]}

    if simulator_name == "nvc":
        # --load is a run (-r) option, so it goes in sim_flags rather than global_flags.
        return {"nvc.sim_flags": [f"--load={lib_name_path('vhpi', 'nvc')}"]}

    if simulator_name == "modelsim":
        if vhdl:
            # VUnit space-joins vsim_flags into a Tcl eval, so the -foreign value needs Tcl braces.
            return {"modelsim.vsim_flags": ["-foreign", f"{{cocotb_init {lib_name_path('fli', 'questa')}}}"]}
        return {"modelsim.vsim_flags": ["-pli", str(lib_name_path("vpi", "questa"))]}

    if simulator_name == "rivierapro":
        flags = ["+access", "+w", "-interceptcoutput", "-O2"]
        if vhdl:
            flags += [f"-loadvhpi {lib_name_path('vhpi', 'riviera')}"]
        else:
            flags += [f"-pli {lib_name_path('vpi', 'riviera')}"]
        return {"rivierapro.vsim_flags": flags}

    raise RuntimeError(f"vcst does not support cocotb tests on simulator {simulator_name!r}")


def get_result_file_name(output_path):
    return str(Path(output_path) / "cocotb_results.xml")
