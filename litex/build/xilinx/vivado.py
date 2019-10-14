# This file is Copyright (c) 2014-2019 Florent Kermarrec <florent@enjoy-digital.fr>
# License: BSD

import os
import subprocess
import sys
import math
from distutils.spawn import find_executable

from migen.fhdl.structure import _Fragment

from litex.build.generic_platform import *
from litex.build import tools
from litex.build.xilinx import common


def _format_constraint(c):
    if isinstance(c, Pins):
        return "set_property LOC " + c.identifiers[0]
    elif isinstance(c, IOStandard):
        return "set_property IOSTANDARD " + c.name
    elif isinstance(c, Drive):
        return "set_property DRIVE " + str(c.strength)
    elif isinstance(c, Misc):
        return "set_property " + c.misc.replace("=", " ")
    elif isinstance(c, Inverted):
        return None
    else:
        raise ValueError("unknown constraint {}".format(c))


def _format_xdc(signame, resname, *constraints):
    fmt_c = [_format_constraint(c) for c in constraints]
    fmt_r = resname[0] + ":" + str(resname[1])
    if resname[2] is not None:
        fmt_r += "." + resname[2]
    r = " ## {}\n".format(fmt_r)
    for c in fmt_c:
        if c is not None:
            r += c + " [get_ports " + signame + "]\n"
    return r


def _build_xdc(named_sc, named_pc):
    r = ""
    for sig, pins, others, resname in named_sc:
        if len(pins) > 1:
            for i, p in enumerate(pins):
                r += _format_xdc(sig + "[" + str(i) + "]", resname, Pins(p), *others)
        elif pins:
            r += _format_xdc(sig, resname, Pins(pins[0]), *others)
        else:
            r += _format_xdc(sig, resname, *others)
    if named_pc:
        r += "\n" + "\n\n".join(named_pc)
    return r


def _run_vivado(build_name, vivado_path, source, ver=None):
    if sys.platform == "win32" or sys.platform == "cygwin":
        build_script_contents = "REM Autogenerated by LiteX / git: " + tools.get_litex_git_revision() + "\n"
        build_script_contents += "vivado -mode batch -source " + build_name + ".tcl\n"
        build_script_file = "build_" + build_name + ".bat"
        tools.write_to_file(build_script_file, build_script_contents)
        command = build_script_file
    else:
        build_script_contents = "# Autogenerated by LiteX / git: " + tools.get_litex_git_revision() + "\nset -e\n"
        # Only source Vivado settings if not already in our $PATH
        if not find_executable("vivado"):
            # For backwards compatibility with ISE paths, also
            # look for a version in a subdirectory named "Vivado"
            # under the current directory.
            paths_to_try = [vivado_path, os.path.join(vivado_path, "Vivado")]
            for p in paths_to_try:
                try:
                    settings = common.settings(p, ver)
                except OSError:
                    continue
                break
            else:
                raise OSError("Unable to locate Vivado directory or settings.")
            build_script_contents += "source " + settings + "\n"

        build_script_contents += "vivado -mode batch -source " + build_name + ".tcl\n"
        build_script_file = "build_" + build_name + ".sh"
        tools.write_to_file(build_script_file, build_script_contents)
        command = ["bash", build_script_file]
    r = tools.subprocess_call_filtered(command, common.colors)
    if r != 0:
        raise OSError("Subprocess failed")


class XilinxVivadoToolchain:
    attr_translate = {
        "keep": ("dont_touch", "true"),
        "no_retiming": ("dont_touch", "true"),
        "async_reg": ("async_reg", "true"),
        "mr_ff": ("mr_ff", "true"),  # user-defined attribute
        "ars_ff1": ("ars_ff1", "true"),  # user-defined attribute
        "ars_ff2": ("ars_ff2", "true"),  # user-defined attribute
        "no_shreg_extract": None
    }

    def __init__(self):
        self.bitstream_commands = []
        self.additional_commands = []
        self.pre_synthesis_commands = []
        self.incremental_implementation = False
        self.vivado_synth_directive = "default"
        self.opt_directive = "default"
        self.vivado_place_directive = "default"
        self.vivado_post_place_phys_opt_directive = None
        self.vivado_route_directive = "default"
        self.vivado_post_route_phys_opt_directive = "default"
        self.clocks = dict()
        self.false_paths = set()

    def _build_batch(self, platform, sources, edifs, ips, build_name, synth_mode, enable_xpm):
        assert synth_mode in ["vivado", "yosys"]
        tcl = []
        tcl.append("create_project -force -name {} -part {}".format(build_name, platform.device))
        tcl.append("set_msg_config -id {Common 17-55} -new_severity {Warning}")
        if enable_xpm:
            tcl.append("set_property XPM_LIBRARIES {XPM_CDC XPM_MEMORY} [current_project]")
        if synth_mode == "vivado":
            # "-include_dirs {}" crashes Vivado 2016.4
            for filename, language, library in sources:
                filename_tcl = "{" + filename + "}"
                if (language == "systemverilog"):
                    tcl.append("read_verilog -sv " + filename_tcl)
                elif (language == "verilog"):
                    tcl.append("read_verilog " + filename_tcl)
                elif (language == "vhdl"):
                    tcl.append("read_vhdl " + filename_tcl)
                    tcl.append("set_property library {} [get_files {}]"
                               .format(library, filename_tcl))
                else:
                    tcl.append("add_files " + filename_tcl)
        for filename in edifs:
            filename_tcl = "{" + filename + "}"
            tcl.append("read_edif " + filename_tcl)

        for filename in ips:
            filename_tcl = "{" + filename + "}"
            ip = os.path.splitext(os.path.basename(filename))[0]
            tcl.append("read_ip " + filename_tcl)
            tcl.append("upgrade_ip [get_ips {}]".format(ip))
            tcl.append("generate_target all [get_ips {}]".format(ip))
            tcl.append("synth_ip [get_ips {}] -force".format(ip))
            tcl.append("get_files -all -of_objects [get_files {}]".format(filename_tcl))

        tcl.append("read_xdc {}.xdc".format(build_name))
        tcl.extend(c.format(build_name=build_name) for c in self.pre_synthesis_commands)

        if synth_mode == "vivado":
            synth_cmd = "synth_design -directive {} -top {} -part {}".format(self.vivado_synth_directive,
                                                                             build_name, platform.device)
            if platform.verilog_include_paths:
                synth_cmd += " -include_dirs {{{}}}".format(" ".join(platform.verilog_include_paths))
            tcl.append(synth_cmd)
        elif synth_mode == "yosys":
            tcl.append("read_edif {}.edif".format(build_name))
            tcl.append("link_design -top {} -part {}".format(build_name, platform.device))
        else:
            raise OSError("Unknown synthesis mode! {}".format(synth_mode))

        tcl.append("report_timing_summary -file {}_timing_synth.rpt".format(build_name))
        tcl.append("report_utilization -hierarchical -file {}_utilization_hierarchical_synth.rpt".format(build_name))
        tcl.append("report_utilization -file {}_utilization_synth.rpt".format(build_name))
        tcl.append("opt_design -directive {}".format(self.opt_directive))
        if self.incremental_implementation:
            tcl.append("read_checkpoint -incremental {}_route.dcp".format(build_name))
        tcl.append("place_design -directive {}".format(self.vivado_place_directive))
        if self.vivado_post_place_phys_opt_directive:
            tcl.append("phys_opt_design -directive {}".format(self.vivado_post_place_phys_opt_directive))
        tcl.append("report_utilization -hierarchical -file {}_utilization_hierarchical_place.rpt".format(build_name))
        tcl.append("report_utilization -file {}_utilization_place.rpt".format(build_name))
        tcl.append("report_io -file {}_io.rpt".format(build_name))
        tcl.append("report_control_sets -verbose -file {}_control_sets.rpt".format(build_name))
        tcl.append("report_clock_utilization -file {}_clock_utilization.rpt".format(build_name))
        tcl.append("route_design -directive {}".format(self.vivado_route_directive))
        tcl.append("phys_opt_design -directive {}".format(self.vivado_post_route_phys_opt_directive))
        tcl.append("report_timing_summary -no_header -no_detailed_paths")
        tcl.append("write_checkpoint -force {}_route.dcp".format(build_name))
        tcl.append("report_route_status -file {}_route_status.rpt".format(build_name))
        tcl.append("report_drc -file {}_drc.rpt".format(build_name))
        tcl.append("report_timing_summary -datasheet -max_paths 10 -file {}_timing.rpt".format(build_name))
        tcl.append("report_power -file {}_power.rpt".format(build_name))
        for bitstream_command in self.bitstream_commands:
            tcl.append(bitstream_command.format(build_name=build_name))
        tcl.append("write_bitstream -force {}.bit ".format(build_name))
        for additional_command in self.additional_commands:
            tcl.append(additional_command.format(build_name=build_name))
        tcl.append("quit")
        tools.write_to_file(build_name + ".tcl", "\n".join(tcl))

    def _convert_clocks(self, platform):
        for clk, period in sorted(self.clocks.items(), key=lambda x: x[0].duid):
            platform.add_platform_command(
                "create_clock -name {clk} -period " + str(period) +
                " [get_nets {clk}]", clk=clk)
        for from_, to in sorted(self.false_paths,
                                key=lambda x: (x[0].duid, x[1].duid)):
            platform.add_platform_command(
                "set_clock_groups "
                "-group [get_clocks -include_generated_clocks -of [get_nets {from_}]] "
                "-group [get_clocks -include_generated_clocks -of [get_nets {to}]] "
                "-asynchronous",
                from_=from_, to=to)

        # make sure add_*_constraint cannot be used again
        del self.clocks
        del self.false_paths

    def _constrain(self, platform):
        # The asynchronous input to a MultiReg is a false path
        platform.add_platform_command(
            "set_false_path -quiet "
            "-to [get_nets -quiet -filter {{mr_ff == TRUE}}]"
        )
        # The asychronous reset input to the AsyncResetSynchronizer is a false
        # path
        platform.add_platform_command(
            "set_false_path -quiet "
            "-to [get_pins -quiet -filter {{REF_PIN_NAME == PRE}} "
                "-of [get_cells -quiet -filter {{ars_ff1 == TRUE || ars_ff2 == TRUE}}]]"
        )
        # clock_period-2ns to resolve metastability on the wire between the
        # AsyncResetSynchronizer FFs
        platform.add_platform_command(
            "set_max_delay 2 -quiet "
            "-from [get_pins -quiet -filter {{REF_PIN_NAME == Q}} "
                "-of [get_cells -quiet -filter {{ars_ff1 == TRUE}}]] "
            "-to [get_pins -quiet -filter {{REF_PIN_NAME == D}} "
                "-of [get_cells -quiet -filter {{ars_ff2 == TRUE}}]]"
        )

    def build(self, platform, fragment, build_dir="build", build_name="top",
            toolchain_path="/opt/Xilinx/Vivado", source=True, run=True,
            synth_mode="vivado", enable_xpm=False, **kwargs):
        if toolchain_path is None:
            toolchain_path = "/opt/Xilinx/Vivado"
        os.makedirs(build_dir, exist_ok=True)
        cwd = os.getcwd()
        os.chdir(build_dir)

        if not isinstance(fragment, _Fragment):
            fragment = fragment.get_fragment()
        platform.finalize(fragment)
        self._convert_clocks(platform)
        self._constrain(platform)
        v_output = platform.get_verilog(fragment, name=build_name, **kwargs)
        named_sc, named_pc = platform.resolve_signals(v_output.ns)
        v_file = build_name + ".v"
        v_output.write(v_file)
        sources = platform.sources | {(v_file, "verilog", "work")}
        edifs = platform.edifs
        ips = platform.ips
        self._build_batch(platform, sources, edifs, ips, build_name, synth_mode, enable_xpm)
        tools.write_to_file(build_name + ".xdc", _build_xdc(named_sc, named_pc))
        if run:
            if synth_mode == "yosys":
                common._run_yosys(platform.device, sources, platform.verilog_include_paths, build_name)
            _run_vivado(build_name, toolchain_path, source)

        os.chdir(cwd)

        return v_output.ns

    def add_period_constraint(self, platform, clk, period):
        if clk in self.clocks:
            raise ValueError("A period constraint already exists")
        period = math.floor(period*1e3)/1e3 # round to lowest picosecond
        self.clocks[clk] = period

    def add_false_path_constraint(self, platform, from_, to):
        if (to, from_) not in self.false_paths:
            self.false_paths.add((from_, to))

def vivado_build_args(parser):
    parser.add_argument("--synth-mode", default="vivado", help="synthesis mode (vivado or yosys, default=vivado)")


def vivado_build_argdict(args):
    return {"synth_mode": args.synth_mode}
