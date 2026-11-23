# Code based on Qiskit 2.5's built-in preset-pass-manager plugins.
#
# The intent of this plugin is deliberately narrow: preserve Qiskit's default
# pipeline and replace only VF2Layout/VF2PostLayout with GlasgowLayout/
# GlasgowPostLayout at the same points in the layout, routing, and optimization
# stages.

from copy import copy
from enum import Enum
import os

from qiskit import user_config
from qiskit.passmanager import ConditionalController
from qiskit.transpiler import AnalysisPass, PassManager
from qiskit.transpiler.exceptions import TranspilerError
from qiskit.transpiler.passes import (
    ApplyLayout,
    BarrierBeforeFinalMeasurements,
    CheckMap,
    FilterOpNodes,
    SabreLayout,
    SabreSwap,
    SetLayout,
    TrivialLayout,
)
from qiskit.transpiler.preset_passmanagers import common
from qiskit.transpiler.preset_passmanagers.builtin_plugins import (
    OptimizationPassManager as QiskitOptimizationPassManager,
)
from qiskit.transpiler.preset_passmanagers.plugin import PassManagerStagePlugin
from qiskit.utils import default_num_processes

from . import _core


CONFIG = user_config.get_config()

# These routing-stage names all use SABRE routing.  The Glasgow names are
# external plugin names, so Qiskit's stock layout-stage check would otherwise
# incorrectly force SabreLayout(skip_routing=True).
_SABRE_ROUTING_METHODS = (None, "default", "sabre", "glasgow", "glasgow_post")


def _get_trial_count(default_trials=5):
    if CONFIG.get("sabre_all_threads", None) or os.getenv("QISKIT_SABRE_ALL_THREADS"):
        return max(default_num_processes(), default_trials)
    return default_trials


def _get_post_layout_limits(
    optimization_level,
    layout_method,
    initial_layout,
    *,
    exact_match=False,
):
    """Use Qiskit's VF2 budgets for the equivalent Glasgow replacement.

    Qiskit's ``common.get_vf2_limits`` intentionally disables the built-in
    VF2PostLayout when an explicit layout plugin is selected.  Since selecting
    ``layout_method="glasgow"`` is how we replace the default VF2 layout stage,
    normalize that one method back to ``None`` when asking Qiskit for the
    budgets that its default workflow would have used.
    """
    normalized_layout_method = None if layout_method == "glasgow" else layout_method
    return common.get_vf2_limits(
        optimization_level,
        normalized_layout_method,
        initial_layout,
        exact_match=exact_match,
    )


class GlasgowLayoutStopReason(Enum):
    SOLUTION_FOUND = "solution found"
    NO_SOLUTION_FOUND = "nonexistent solution"


class GlasgowLayout(AnalysisPass):
    def __init__(self, target):
        super().__init__()
        self.target = target

    def run(self, dag):
        if self.target is None:
            layout = None
        else:
            layout = _core.glasgow_mapping(
                dag=dag,
                target=self.target,
            )

        found = layout is not None
        self.property_set["GlasgowLayout_found"] = found
        self.property_set["GlasgowLayout_stop_reason"] = (
            GlasgowLayoutStopReason.SOLUTION_FOUND
            if found
            else GlasgowLayoutStopReason.NO_SOLUTION_FOUND
        )
        if found:
            self.property_set["layout"] = layout

        return None


class GlasgowPostLayout(AnalysisPass):
    def __init__(
        self,
        target,
        seed=-1,
        call_limit=None,
        max_trials=0,
        strict_direction=False,
    ):
        super().__init__()
        self.target = target
        # Retain the same constructor surface as VF2PostLayout so the stage
        # plugins can mirror upstream Qiskit exactly.  Glasgow currently uses
        # its own internal search termination policy; seed/call_limit/max_trials
        # are recorded here for API/stage parity.
        self.seed = seed
        self.call_limit = call_limit
        self.max_trials = max_trials
        self.strict_direction = strict_direction

    def run(self, dag):
        if self.target is None:
            return None

        layout = _core.glasgow_mapping_post(
            dag=dag,
            target=self.target,
            strict_direction=self.strict_direction,
        )

        if layout is not None:
            self.property_set["post_layout"] = layout

        return None


class GlasgowLayoutPassManager(PassManagerStagePlugin):
    """Qiskit's default layout stage with VF2Layout replaced by GlasgowLayout."""

    def pass_manager(self, pass_manager_config, optimization_level=None) -> PassManager:
        _given_layout = SetLayout(pass_manager_config.initial_layout)

        def _choose_layout_condition(property_set):
            return not property_set["layout"]

        def _layout_not_perfect(property_set):
            return (
                property_set["is_swap_mapped"] is not None
                and not property_set["is_swap_mapped"]
            )

        def _glasgow_match_not_found(property_set):
            if property_set["layout"] is None:
                return True
            return (
                property_set["GlasgowLayout_stop_reason"] is not None
                and property_set["GlasgowLayout_stop_reason"]
                is not GlasgowLayoutStopReason.SOLUTION_FOUND
            )

        def _swap_mapped(property_set):
            return property_set["final_layout"] is None

        if pass_manager_config.target is None:
            coupling_map = pass_manager_config.coupling_map
        else:
            coupling_map = pass_manager_config.target

        layout = PassManager()
        layout.append(_given_layout)

        # Exact copy of Qiskit's optimization-level-0 behavior: VF2 is not
        # present at this level, so there is nothing to replace.
        if optimization_level == 0:
            if coupling_map is not None:
                layout.append(
                    ConditionalController(
                        TrivialLayout(coupling_map),
                        condition=_choose_layout_condition,
                    )
                )
            layout += common.generate_embed_passmanager(coupling_map)
            return layout

        if coupling_map is None:
            pass

        elif optimization_level == 1:
            layout.append(
                ConditionalController(
                    [TrivialLayout(coupling_map), CheckMap(coupling_map)],
                    condition=_choose_layout_condition,
                )
            )

            choose_layout_1 = GlasgowLayout(pass_manager_config.target)
            layout.append(
                ConditionalController(
                    choose_layout_1,
                    condition=_layout_not_perfect,
                )
            )

            trial_count = _get_trial_count(5)
            choose_layout_2 = SabreLayout(
                coupling_map,
                max_iterations=2,
                seed=pass_manager_config.seed_transpiler,
                swap_trials=trial_count,
                layout_trials=trial_count,
                skip_routing=pass_manager_config.routing_method
                not in _SABRE_ROUTING_METHODS,
            )
            layout.append(
                ConditionalController(
                    [
                        BarrierBeforeFinalMeasurements(
                            "qiskit.transpiler.internal.routing.protection.barrier"
                        ),
                        choose_layout_2,
                    ],
                    condition=_glasgow_match_not_found,
                )
            )

        elif optimization_level == 2:
            choose_layout_0 = GlasgowLayout(pass_manager_config.target)
            layout.append(
                ConditionalController(
                    choose_layout_0,
                    condition=_choose_layout_condition,
                )
            )

            trial_count = _get_trial_count(20)
            choose_layout_1 = SabreLayout(
                coupling_map,
                max_iterations=2,
                seed=pass_manager_config.seed_transpiler,
                swap_trials=trial_count,
                layout_trials=trial_count,
                skip_routing=pass_manager_config.routing_method
                not in _SABRE_ROUTING_METHODS,
            )
            layout.append(
                ConditionalController(
                    [
                        BarrierBeforeFinalMeasurements(
                            "qiskit.transpiler.internal.routing.protection.barrier"
                        ),
                        choose_layout_1,
                    ],
                    condition=_glasgow_match_not_found,
                )
            )

        elif optimization_level == 3:
            choose_layout_0 = GlasgowLayout(pass_manager_config.target)
            layout.append(
                ConditionalController(
                    choose_layout_0,
                    condition=_choose_layout_condition,
                )
            )

            trial_count = _get_trial_count(20)
            choose_layout_1 = SabreLayout(
                coupling_map,
                max_iterations=4,
                seed=pass_manager_config.seed_transpiler,
                swap_trials=trial_count,
                layout_trials=trial_count,
                skip_routing=pass_manager_config.routing_method
                not in _SABRE_ROUTING_METHODS,
            )
            layout.append(
                ConditionalController(
                    [
                        BarrierBeforeFinalMeasurements(
                            "qiskit.transpiler.internal.routing.protection.barrier"
                        ),
                        choose_layout_1,
                    ],
                    condition=_glasgow_match_not_found,
                )
            )

        else:
            raise TranspilerError(f"Invalid optimization level: {optimization_level}")

        embed = common.generate_embed_passmanager(coupling_map)
        layout.append(
            ConditionalController(
                embed.to_flow_controller(),
                condition=_swap_mapped,
            )
        )
        return layout


def _generate_glasgow_routing_passmanager(
    routing_pass,
    target,
    coupling_map=None,
    glasgow_call_limit=None,
    seed_transpiler=-1,
    check_trivial=False,
    use_barrier_before_measurement=True,
    glasgow_max_trials=None,
):
    """Qiskit's generate_routing_passmanager with VF2PostLayout replaced."""

    def _run_post_layout_condition(property_set):
        if not check_trivial or (
            property_set["is_swap_mapped"] is not None
            and not property_set["is_swap_mapped"]
        ):
            stop_reason = property_set["GlasgowLayout_stop_reason"]
            if (
                stop_reason is None
                or stop_reason is not GlasgowLayoutStopReason.SOLUTION_FOUND
            ):
                return True
        return False

    routing = PassManager()
    if target is not None:
        routing.append(CheckMap(target, property_set_field="routing_not_needed"))
    else:
        routing.append(
            CheckMap(coupling_map, property_set_field="routing_not_needed")
        )

    def _swap_condition(property_set):
        return not property_set["routing_not_needed"]

    if use_barrier_before_measurement:
        routing.append(
            ConditionalController(
                [
                    BarrierBeforeFinalMeasurements(
                        label="qiskit.transpiler.internal.routing.protection.barrier"
                    ),
                    routing_pass,
                ],
                condition=_swap_condition,
            )
        )
    else:
        routing.append(
            ConditionalController(
                routing_pass,
                condition=_swap_condition,
            )
        )

    is_glasgow_fully_bounded = glasgow_call_limit and glasgow_max_trials
    if target is not None and is_glasgow_fully_bounded:
        routing.append(
            ConditionalController(
                GlasgowPostLayout(
                    target,
                    seed=seed_transpiler,
                    call_limit=glasgow_call_limit,
                    max_trials=glasgow_max_trials,
                    strict_direction=False,
                ),
                condition=_run_post_layout_condition,
            )
        )
        routing.append(
            ConditionalController(
                ApplyLayout(),
                condition=common._apply_post_layout_condition,
            )
        )

    def filter_fn(node):
        return node.label != "qiskit.transpiler.internal.routing.protection.barrier"

    routing.append([FilterOpNodes(filter_fn)])
    return routing


class GlasgowRoutingPassManager(PassManagerStagePlugin):
    """Qiskit's SABRE routing stage with VF2PostLayout replaced by GlasgowPostLayout."""

    def pass_manager(self, pass_manager_config, optimization_level=None) -> PassManager:
        seed_transpiler = pass_manager_config.seed_transpiler
        target = pass_manager_config.target
        coupling_map = pass_manager_config.coupling_map
        coupling_map_routing = target if target is not None else coupling_map

        glasgow_call_limit, glasgow_max_trials = _get_post_layout_limits(
            optimization_level,
            pass_manager_config.layout_method,
            pass_manager_config.initial_layout,
        )

        if optimization_level == 0:
            trial_count = _get_trial_count(5)
            routing_pass = SabreSwap(
                coupling_map_routing,
                heuristic="basic",
                seed=seed_transpiler,
                trials=trial_count,
            )
            return _generate_glasgow_routing_passmanager(
                routing_pass,
                target,
                coupling_map=coupling_map,
                seed_transpiler=-1,
                use_barrier_before_measurement=True,
            )

        if optimization_level == 1:
            trial_count = _get_trial_count(5)
            routing_pass = SabreSwap(
                coupling_map_routing,
                heuristic="decay",
                seed=seed_transpiler,
                trials=trial_count,
            )
            return _generate_glasgow_routing_passmanager(
                routing_pass,
                target,
                coupling_map,
                glasgow_call_limit=glasgow_call_limit,
                glasgow_max_trials=glasgow_max_trials,
                seed_transpiler=-1,
                check_trivial=True,
                use_barrier_before_measurement=True,
            )

        if optimization_level == 2:
            trial_count = _get_trial_count(20)
            routing_pass = SabreSwap(
                coupling_map_routing,
                heuristic="decay",
                seed=seed_transpiler,
                trials=trial_count,
            )
            return _generate_glasgow_routing_passmanager(
                routing_pass,
                target,
                coupling_map=coupling_map,
                glasgow_call_limit=glasgow_call_limit,
                glasgow_max_trials=glasgow_max_trials,
                seed_transpiler=-1,
                use_barrier_before_measurement=True,
            )

        if optimization_level == 3:
            trial_count = _get_trial_count(20)
            routing_pass = SabreSwap(
                coupling_map_routing,
                heuristic="decay",
                seed=seed_transpiler,
                trials=trial_count,
            )
            return _generate_glasgow_routing_passmanager(
                routing_pass,
                target,
                coupling_map=coupling_map,
                glasgow_call_limit=glasgow_call_limit,
                glasgow_max_trials=glasgow_max_trials,
                seed_transpiler=-1,
                use_barrier_before_measurement=True,
            )

        raise TranspilerError(
            f"Invalid optimization level specified: {optimization_level}"
        )


class GlasgowOptimizationPassManager(PassManagerStagePlugin):
    """Qiskit's optimization stage with its level-3 VF2PostLayout replaced."""

    def pass_manager(self, pass_manager_config, optimization_level=None):
        # Let upstream Qiskit build the complete optimization stage.  Force an
        # explicit layout method in the copied config so Qiskit's own
        # VF2PostLayout post-loop is suppressed; we append GlasgowPostLayout in
        # the exact same post-loop position below.
        upstream_config = copy(pass_manager_config)
        upstream_config.layout_method = "glasgow"

        optimization = QiskitOptimizationPassManager().pass_manager(
            upstream_config,
            optimization_level=optimization_level,
        )

        if optimization_level != 3:
            return optimization

        if (
            pass_manager_config.coupling_map
            and pass_manager_config.target is not None
        ):
            glasgow_call_limit, glasgow_max_trials = _get_post_layout_limits(
                optimization_level,
                pass_manager_config.layout_method,
                pass_manager_config.initial_layout,
                exact_match=True,
            )
            if glasgow_call_limit and glasgow_max_trials:
                optimization.append(
                    [
                        GlasgowPostLayout(
                            target=pass_manager_config.target,
                            seed=-1,
                            call_limit=glasgow_call_limit,
                            max_trials=glasgow_max_trials,
                            strict_direction=True,
                        ),
                        ConditionalController(
                            ApplyLayout(),
                            condition=common._apply_post_layout_condition,
                        ),
                    ]
                )

        return optimization


# Backward-compatible class name used by the original routing entry point.
GlasgowRouting = GlasgowRoutingPassManager
