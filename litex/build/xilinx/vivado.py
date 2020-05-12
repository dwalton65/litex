# This file is Copyright (c) 2014-2020 Florent Kermarrec <florent@enjoy-digital.fr>
# License: BSD

import os
import subprocess
import sys
import math

from migen.fhdl.structure import _Fragment

from litex.build.generic_platform import *
from litex.build import tools
from litex.build.xilinx import common

# Constraints (.xdc) -------------------------------------------------------------------------------

def _xdc_separator(msg):
    r =  "#"*80 + "\n"
    r += "# " + msg + "\n"
    r += "#"*80 + "\n"
    return r

def _format_xdc_constraint(c):
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
    fmt_c = [_format_xdc_constraint(c) for c in constraints]
    fmt_r = resname[0] + ":" + str(resname[1])
    if resname[2] is not None:
        fmt_r += "." + resname[2]
    r = "# {}\n".format(fmt_r)
    for c in fmt_c:
        if c is not None:
            r += c + " [get_ports " + signame + "]\n"
    r += "\n"
    return r


def _build_xdc(named_sc, named_pc):
    r = _xdc_separator("IO constraints")
    for sig, pins, others, resname in named_sc:
        if len(pins) > 1:
            for i, p in enumerate(pins):
                r += _format_xdc(sig + "[" + str(i) + "]", resname, Pins(p), *others)
        elif pins:
            r += _format_xdc(sig, resname, Pins(pins[0]), *others)
        else:
            r += _format_xdc(sig, resname, *others)
    r += _xdc_separator("Design constraints")
    if named_pc:
        r += "\n" + "\n\n".join(named_pc)
    return r

# Script -------------------------------------------------------------------------------------------

def _build_script(build_name):
    if sys.platform in ["win32", "cygwin"]:
        script_contents = "REM Autogenerated by LiteX / git: " + tools.get_litex_git_revision() + "\n"
        script_contents += "vivado -mode batch -source " + build_name + ".tcl\n"
        script_file = "build_" + build_name + ".bat"
        tools.write_to_file(script_file, script_contents)
    else:
        script_contents = "# Autogenerated by LiteX / git: " + tools.get_litex_git_revision() + "\nset -e\n"
        script_contents += "vivado -mode batch -source " + build_name + ".tcl\n"
        script_file = "build_" + build_name + ".sh"
        tools.write_to_file(script_file, script_contents)
    return script_file

def _run_script(script):
    if sys.platform in ["win32", "cygwin"]:
        shell = ["cmd", "/c"]
    else:
        shell = ["bash"]

    if tools.subprocess_call_filtered(shell + [script], common.colors) != 0:
        raise OSError("Subprocess failed")

# XilinxVivadoToolchain ----------------------------------------------------------------------------

class XilinxVivadoToolchain:
    attr_translate = {
        "keep":            ("dont_touch", "true"),
        "no_retiming":     ("dont_touch", "true"),
        "async_reg":       ("async_reg",  "true"),
        "mr_ff":           ("mr_ff",      "true"), # user-defined attribute
        "ars_ff1":         ("ars_ff1",    "true"), # user-defined attribute
        "ars_ff2":         ("ars_ff2",    "true"), # user-defined attribute
        "no_shreg_extract": None
    }

    def __init__(self):
        self.bitstream_commands                   = []
        self.additional_commands                  = []
        self.pre_synthesis_commands               = []
        self.pre_placement_commands               = []
        self.pre_routing_commands                 = []
        self.incremental_implementation           = False
        self.vivado_synth_directive               = "default"
        self.opt_directive                        = "default"
        self.vivado_place_directive               = "default"
        self.vivado_post_place_phys_opt_directive = None
        self.vivado_route_directive               = "default"
        self.vivado_post_route_phys_opt_directive = "default"
        self.clocks      = dict()
        self.false_paths = set()

    def _build_tcl(self, platform, build_name, synth_mode, enable_xpm):
        assert synth_mode in ["vivado", "yosys"]
        tcl = []

        # Create project
        tcl.append("\n# Create Project\n")
        tcl.append("create_project -force -name {} -part {}".format(build_name, platform.device))
        tcl.append("set_msg_config -id {Common 17-55} -new_severity {Warning}")

        # Enable Xilinx Parameterized Macros
        if enable_xpm:
            tcl.append("\n# Enable Xilinx Parameterized Macros\n")
            tcl.append("set_property XPM_LIBRARIES {XPM_CDC XPM_MEMORY} [current_project]")

        # Add sources (when Vivado used for synthesis)
        if synth_mode == "vivado":
            tcl.append("\n# Add Sources\n")
            # "-include_dirs {}" crashes Vivado 2016.4
            for filename, language, library in platform.sources:
                filename_tcl = "{" + filename + "}"
                if (language == "systemverilog"):
                    tcl.append("read_verilog -v " + filename_tcl)
                    tcl.append("set_property file_type SystemVerilog [get_files {}]"
                            .format(filename_tcl))
                elif (language == "verilog"):
                    tcl.append("read_verilog " + filename_tcl)
                elif (language == "vhdl"):
                    tcl.append("read_vhdl -vhdl2008 " + filename_tcl)
                    tcl.append("set_property library {} [get_files {}]"
                               .format(library, filename_tcl))
                else:
                    tcl.append("add_files " + filename_tcl)

        # Add EDIFs
        tcl.append("\n# Add EDIFs\n")
        for filename in platform.edifs:
            filename_tcl = "{" + filename + "}"
            tcl.append("read_edif " + filename_tcl)

        # Add IPs
        tcl.append("\n# Add IPs\n")
        for filename, disable_constraints in platform.ips.items():
            filename_tcl = "{" + filename + "}"
            ip = os.path.splitext(os.path.basename(filename))[0]
            tcl.append("read_ip " + filename_tcl)
            tcl.append("upgrade_ip [get_ips {}]".format(ip))
            tcl.append("generate_target all [get_ips {}]".format(ip))
            tcl.append("synth_ip [get_ips {}] -force".format(ip))
            tcl.append("get_files -all -of_objects [get_files {}]".format(filename_tcl))
            if disable_constraints:
                tcl.append("set_property is_enabled false [get_files -of_objects [get_files {}] -filter {{FILE_TYPE == XDC}}]".format(filename_tcl))

        # Add constraints
        tcl.append("\n# Add constraints\n")
        tcl.append("read_xdc {}.xdc".format(build_name))
        tcl.append("set_property PROCESSING_ORDER EARLY [get_files {}.xdc]".format(build_name))

        # Add pre-synthesis commands
        tcl.append("\n# Add pre-synthesis commands\n")
        tcl.extend(c.format(build_name=build_name) for c in self.pre_synthesis_commands)

        # Synthesis
        if synth_mode == "vivado":
            tcl.append("\n# Synthesis\n")
            synth_cmd = "synth_design -directive {} -top {} -part {}".format(self.vivado_synth_directive,
                                                                             build_name, platform.device)
            if platform.verilog_include_paths:
                synth_cmd += " -include_dirs {{{}}}".format(" ".join(platform.verilog_include_paths))
            tcl.append(synth_cmd)
        elif synth_mode == "yosys":
            tcl.append("\n# Read Yosys EDIF\n")
            tcl.append("read_edif {}.edif".format(build_name))
            tcl.append("link_design -top {} -part {}".format(build_name, platform.device))
        else:
            raise OSError("Unknown synthesis mode! {}".format(synth_mode))
        tcl.append("\n# Synthesis report\n")
        tcl.append("report_timing_summary -file {}_timing_synth.rpt".format(build_name))
        tcl.append("report_utilization -hierarchical -file {}_utilization_hierarchical_synth.rpt".format(build_name))
        tcl.append("report_utilization -file {}_utilization_synth.rpt".format(build_name))

        # Optimize
        tcl.append("\n# Optimize design\n")
        tcl.append("opt_design -directive {}".format(self.opt_directive))

        # Incremental implementation
        if self.incremental_implementation:
            tcl.append("\n# Read design checkpoint\n")
            tcl.append("read_checkpoint -incremental {}_route.dcp".format(build_name))

        # Add pre-placement commands
        tcl.append("\n# Add pre-placement commands\n")
        tcl.extend(c.format(build_name=build_name) for c in self.pre_placement_commands)

        # Placement
        tcl.append("\n# Placement\n")
        tcl.append("place_design -directive {}".format(self.vivado_place_directive))
        if self.vivado_post_place_phys_opt_directive:
            tcl.append("phys_opt_design -directive {}".format(self.vivado_post_place_phys_opt_directive))
        tcl.append("\n# Placement report\n")
        tcl.append("report_utilization -hierarchical -file {}_utilization_hierarchical_place.rpt".format(build_name))
        tcl.append("report_utilization -file {}_utilization_place.rpt".format(build_name))
        tcl.append("report_io -file {}_io.rpt".format(build_name))
        tcl.append("report_control_sets -verbose -file {}_control_sets.rpt".format(build_name))
        tcl.append("report_clock_utilization -file {}_clock_utilization.rpt".format(build_name))

        # Add pre-routing commands
        tcl.append("\n# Add pre-routing commands\n")
        tcl.extend(c.format(build_name=build_name) for c in self.pre_routing_commands)

        # Routing
        tcl.append("\n# Routing\n")
        tcl.append("route_design -directive {}".format(self.vivado_route_directive))
        tcl.append("phys_opt_design -directive {}".format(self.vivado_post_route_phys_opt_directive))
        tcl.append("write_checkpoint -force {}_route.dcp".format(build_name))
        tcl.append("\n# Routing report\n")
        tcl.append("report_timing_summary -no_header -no_detailed_paths")
        tcl.append("report_route_status -file {}_route_status.rpt".format(build_name))
        tcl.append("report_drc -file {}_drc.rpt".format(build_name))
        tcl.append("report_timing_summary -datasheet -max_paths 10 -file {}_timing.rpt".format(build_name))
        tcl.append("report_power -file {}_power.rpt".format(build_name))
        for bitstream_command in self.bitstream_commands:
            tcl.append(bitstream_command.format(build_name=build_name))

        # Bitstream generation
        tcl.append("\n# Bitstream generation\n")
        tcl.append("write_bitstream -force {}.bit ".format(build_name))
        for additional_command in self.additional_commands:
            tcl.append(additional_command.format(build_name=build_name))

        # Quit
        tcl.append("\n# End\n")
        tcl.append("quit")
        tools.write_to_file(build_name + ".tcl", "\n".join(tcl))

    def _build_clock_constraints(self, platform):
        platform.add_platform_command(_xdc_separator("Clock constraints"))
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
        # Make sure add_*_constraint cannot be used again
        del self.clocks
        del self.false_paths

    def _build_false_path_constraints(self, platform):
        platform.add_platform_command(_xdc_separator("False path constraints"))
        # The asynchronous input to a MultiReg is a false path
        platform.add_platform_command(
            "set_false_path -quiet "
            "-through [get_nets -hierarchical -filter {{mr_ff == TRUE}}]"
        )
        # The asychronous reset input to the AsyncResetSynchronizer is a false path
        platform.add_platform_command(
            "set_false_path -quiet "
            "-to [get_pins -filter {{REF_PIN_NAME == PRE}} "
                "-of_objects [get_cells -hierarchical -filter {{ars_ff1 == TRUE || ars_ff2 == TRUE}}]]"
        )
        # clock_period-2ns to resolve metastability on the wire between the AsyncResetSynchronizer FFs
        platform.add_platform_command(
            "set_max_delay 2 -quiet "
            "-from [get_pins -filter {{REF_PIN_NAME == C}} "
                "-of_objects [get_cells -hierarchical -filter {{ars_ff1 == TRUE}}]] "
            "-to [get_pins -filter {{REF_PIN_NAME == D}} "
                "-of_objects [get_cells -hierarchical -filter {{ars_ff2 == TRUE}}]]"
        )


    def build(self, platform, fragment,
        build_dir  = "build",
        build_name = "top",
        run        = True,
        synth_mode = "vivado",
        enable_xpm = False,
        **kwargs):

        # Create build directory
        os.makedirs(build_dir, exist_ok=True)
        cwd = os.getcwd()
        os.chdir(build_dir)

        # Finalize design
        if not isinstance(fragment, _Fragment):
            fragment = fragment.get_fragment()
        platform.finalize(fragment)

        # Generate timing constraints
        self._build_clock_constraints(platform)
        self._build_false_path_constraints(platform)

        # Generate verilog
        v_output = platform.get_verilog(fragment, name=build_name, **kwargs)
        named_sc, named_pc = platform.resolve_signals(v_output.ns)
        v_file = build_name + ".v"
        v_output.write(v_file)
        platform.add_source(v_file)

        # Generate design project (.tcl)
        self._build_tcl(
            platform   = platform,
            build_name = build_name,
            synth_mode = synth_mode,
            enable_xpm = enable_xpm
        )

        # Generate design constraints (.xdc)
        tools.write_to_file(build_name + ".xdc", _build_xdc(named_sc, named_pc))

        # Run
        if run:
            if synth_mode == "yosys":
                common._run_yosys(platform.device, platform.sources, platform.verilog_include_paths, build_name)
            script = _build_script(build_name)
            _run_script(script)

        os.chdir(cwd)

        return v_output.ns

    def add_period_constraint(self, platform, clk, period):
        clk.attr.add("keep")
        period = math.floor(period*1e3)/1e3 # round to lowest picosecond
        if clk in self.clocks:
            if period != self.clocks[clk]:
                raise ValueError("Clock already constrained to {:.2f}ns, new constraint to {:.2f}ns"
                    .format(self.clocks[clk], period))
        self.clocks[clk] = period

    def add_false_path_constraint(self, platform, from_, to):
        from_.attr.add("keep")
        to.attr.add("keep")
        if (to, from_) not in self.false_paths:
            self.false_paths.add((from_, to))

def vivado_build_args(parser):
    parser.add_argument("--synth-mode", default="vivado", help="synthesis mode (vivado or yosys, default=vivado)")


def vivado_build_argdict(args):
    return {"synth_mode": args.synth_mode}
