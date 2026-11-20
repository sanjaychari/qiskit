# Code based on default plugin structure https://github.com/Qiskit/qiskit/blob/stable/2.5/qiskit/transpiler/preset_passmanagers/builtin_plugins.py

import os

from qiskit.transpiler.preset_passmanagers.plugin import PassManagerStagePlugin, PassManagerStagePluginManager
from qiskit.transpiler.preset_passmanagers import common
from qiskit.transpiler import PassManager, AnalysisPass
from qiskit.passmanager import ConditionalController
from qiskit.transpiler.passes import SabreLayout, SetLayout, BarrierBeforeFinalMeasurements, ApplyLayout
from qiskit.transpiler.exceptions import TranspilerError
from qiskit.utils import default_num_processes
from qiskit import user_config

from . import _core

CONFIG = user_config.get_config()

def _get_trial_count(default_trials=5):
    if CONFIG.get("sabre_all_threads", None) or os.getenv("QISKIT_SABRE_ALL_THREADS"):
        return max(default_num_processes(), default_trials)
    return default_trials

class GlasgowLayout(AnalysisPass):
    def __init__(self, target):
        super().__init__()
        self.target = target

    def run(self, dag):
        layout = _core.glasgow_mapping(
            dag=dag,
            target=self.target
        )

        if layout is not None:
            self.property_set["layout"] = layout
        
        self.property_set["GlasgowLayout_found"] = layout is not None

        return None

class GlasgowPostLayout(AnalysisPass):
    def __init__(self, target):
            super().__init__()
            self.target = target
    
    def run(self, dag):
        layout = _core.glasgow_mapping_post(
            dag=dag,
            target=self.target
        )

        if layout is not None:
            self.property_set["post_layout"] = layout

        return None
    

class GlasgowLayoutPassManager(PassManagerStagePlugin):
    def pass_manager(self, pass_manager_config, optimization_level=None) -> PassManager:
        
        _given_layout = SetLayout(pass_manager_config.initial_layout)
        
        def _choose_layout_condition(property_set):
            return property_set["layout"] is None
        
        def _swap_mapped(property_set):
            return property_set["final_layout"] is None
        
        def _glasgow_match_not_found(property_set):
            return (
                property_set["layout"] is None
                or property_set["GlasgowLayout_found"] is False
            )
        
        target = pass_manager_config.target
        device = target if target is not None else pass_manager_config.coupling_map
        
        layout = PassManager()
        layout.append(_given_layout)
        
        if target is None:
            layout_pass = None

        elif optimization_level == 0:
            print("TODO or not TODO...idk")
            
        elif optimization_level == 1:
            print("TODO or not TODO...idk")
            
        elif optimization_level == 2:
            print("TODO or not TODO...idk")
            
        elif optimization_level == 3:
            choose_layout_0 = GlasgowLayout(target)
            layout.append(ConditionalController(choose_layout_0, condition=_choose_layout_condition))
            
            trial_count = _get_trial_count(20)
            choose_layout_1 = SabreLayout(
                device,
                max_iterations=4,
                seed=pass_manager_config.seed_transpiler,
                swap_trials=trial_count,
                layout_trials=trial_count,
                skip_routing=pass_manager_config.routing_method not in (None, "default", "sabre"),
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

        embed = common.generate_embed_passmanager(target)
        layout.append(ConditionalController(embed.to_flow_controller(), condition=_swap_mapped))

        return layout

class GlasgowRouting(PassManagerStagePlugin):
    def pass_manager(self, pass_manager_config, optimization_level=None):
        target = pass_manager_config.target
        
        def _post_layout_found(property_set):
            return property_set["post_layout"] is not None
        
        routing = PassManagerStagePluginManager().get_passmanager_stage(
            "routing",
            "sabre",
            pass_manager_config,
            optimization_level=optimization_level,
        )
        
        routing.append(GlasgowPostLayout(target))
        
        routing.append(
            ConditionalController(
                ApplyLayout(),
                condition=_post_layout_found,
            )
        )
        
        return routing