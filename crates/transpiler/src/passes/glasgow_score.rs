// This code is part of Qiskit.
//
// (C) Copyright IBM 2026.
//
// This code is licensed under the Apache License, Version 2.0. You may
// obtain a copy of this license in the LICENSE.txt file in the root directory
// of this source tree or at https://www.apache.org/licenses/LICENSE-2.0.
//
// Any modifications or derivative works of this code must retain this
// copyright notice, and modified files need to carry a notice indicating
// that they have been altered from the originals.

//! Rust-side batch scoring for candidate mappings returned by Glasgow.
//!
//! Glasgow produces structural subgraph-isomorphism mappings.  The expensive part of the
//! experimental Python integration was rescanning every operation in the routed DAG for every
//! candidate mapping.  This module instead compresses the DAG into weighted one- and two-qubit
//! interactions once, builds compact target-cost tables once, and scores all candidate mappings
//! against those summaries in Rust.

use std::collections::HashMap;

use pyo3::prelude::*;
use pyo3::wrap_pyfunction;

use qiskit_circuit::PhysicalQubit;
use qiskit_circuit::dag_circuit::{DAGCircuit, PyDAGCircuit};
use qiskit_circuit::operations::{ControlFlowView, Operation};

use crate::target::{QargsRef, Target};

const SCORE_EPSILON: f64 = 1e-15;

#[derive(Default)]
struct InteractionWeight {
    total: usize,
    by_name: HashMap<String, usize>,
}

impl InteractionWeight {
    fn add(&mut self, name: &str, repeats: usize, track_names: bool) {
        self.total += repeats;
        if track_names {
            *self.by_name.entry(name.to_string()).or_default() += repeats;
        }
    }
}

#[derive(Default)]
struct CompactInteractions {
    one_q: HashMap<usize, InteractionWeight>,
    two_q: HashMap<(usize, usize), InteractionWeight>,
}

impl CompactInteractions {
    fn from_dag(dag: &DAGCircuit, track_names: bool) -> PyResult<Self> {
        let wire_map = (0..dag.num_qubits()).collect::<Vec<_>>();
        let mut out = Self::default();
        out.add_from_dag(dag, &wire_map, 1, track_names)?;
        Ok(out)
    }

    fn add_from_dag(
        &mut self,
        dag: &DAGCircuit,
        wire_map: &[usize],
        repeats: usize,
        track_names: bool,
    ) -> PyResult<()> {
        for (_, inst) in dag.op_nodes(false) {
            if inst.op.name() == "barrier" {
                continue;
            }

            let qubits = dag.get_qargs(inst.qubits);
            if let Some(control_flow) = dag.try_view_control_flow(inst) {
                let repeats = if let ControlFlowView::ForLoop { collection, .. } = control_flow {
                    repeats * collection.len()
                } else {
                    repeats
                };
                let inner_wire_map = qubits
                    .iter()
                    .map(|qubit| wire_map[qubit.index()])
                    .collect::<Vec<_>>();
                for block in control_flow.blocks() {
                    self.add_from_dag(block, &inner_wire_map, repeats, track_names)?;
                }
                continue;
            }

            match qubits {
                [] => {}
                [qubit] => {
                    let virtual_qubit = wire_map[qubit.index()];
                    self.one_q
                        .entry(virtual_qubit)
                        .or_default()
                        .add(inst.op.name(), repeats, track_names);
                }
                [left, right] => {
                    let left = wire_map[left.index()];
                    let right = wire_map[right.index()];
                    self.two_q
                        .entry((left, right))
                        .or_default()
                        .add(inst.op.name(), repeats, track_names);
                }
                _ => {
                    return Err(pyo3::exceptions::PyValueError::new_err(
                        "Glasgow mapping scoring only supports operations on at most two qubits",
                    ));
                }
            }
        }
        Ok(())
    }
}

fn neg_log_fidelity(error: f64) -> f64 {
    if error.is_nan() || error <= 0.0 {
        0.0
    } else if error >= 1.0 {
        f64::INFINITY
    } else {
        -(-error).ln_1p()
    }
}

fn average_qarg_cost(target: &Target, qargs: &[PhysicalQubit]) -> Option<f64> {
    let qargs_ref = QargsRef::Concrete(qargs);
    let mut names = target
        .operation_names_for_qargs(qargs_ref)
        .ok()?
        .into_iter()
        .collect::<Vec<_>>();
    if names.is_empty() {
        return None;
    }
    names.sort_unstable();

    let total_error = names
        .iter()
        .map(|name| target.get_error(name, qargs_ref).unwrap_or(0.0))
        .sum::<f64>();
    Some(neg_log_fidelity(total_error / names.len() as f64))
}

fn exact_qarg_costs(target: &Target, qargs: &[PhysicalQubit]) -> Option<HashMap<String, f64>> {
    let qargs_ref = QargsRef::Concrete(qargs);
    let names = target.operation_names_for_qargs(qargs_ref).ok()?;
    if names.is_empty() {
        return None;
    }

    Some(
        names
            .into_iter()
            .map(|name| {
                (
                    name.to_string(),
                    neg_log_fidelity(target.get_error(name, qargs_ref).unwrap_or(0.0)),
                )
            })
            .collect(),
    )
}

struct AverageTargetCosts {
    one_q: Vec<Option<f64>>,
    two_q: HashMap<(usize, usize), f64>,
}

impl AverageTargetCosts {
    fn new(target: &Target) -> Self {
        let num_physical = target.num_qubits.unwrap_or_default() as usize;

        let one_q = (0..num_physical)
            .map(|physical| {
                let qargs = [PhysicalQubit::new(physical as u32)];
                average_qarg_cost(target, &qargs)
            })
            .collect();

        let mut two_q = HashMap::new();
        if let Some(qargs_iter) = target.qargs() {
            for qargs in qargs_iter {
                let QargsRef::Concrete(qargs) = qargs.as_ref() else {
                    continue;
                };
                if qargs.len() != 2 {
                    continue;
                }
                if let Some(cost) = average_qarg_cost(target, qargs) {
                    two_q.insert((qargs[0].index(), qargs[1].index()), cost);
                }
            }
        }

        Self { one_q, two_q }
    }

    fn score(&self, interactions: &CompactInteractions, mapping: &[usize]) -> Option<f64> {
        let mut score = 0.0;

        for (virtual_qubit, interaction) in &interactions.one_q {
            let physical = *mapping.get(*virtual_qubit)?;
            let cost = self.one_q.get(physical)?.as_ref()?;
            score += *cost * interaction.total as f64;
        }

        for ((left, right), interaction) in &interactions.two_q {
            let physical_left = *mapping.get(*left)?;
            let physical_right = *mapping.get(*right)?;
            let cost = self
                .two_q
                .get(&(physical_left, physical_right))
                .or_else(|| self.two_q.get(&(physical_right, physical_left)))?;
            score += *cost * interaction.total as f64;
        }

        Some(score)
    }
}

struct ExactTargetCosts {
    one_q: Vec<Option<HashMap<String, f64>>>,
    two_q: HashMap<(usize, usize), HashMap<String, f64>>,
}

impl ExactTargetCosts {
    fn new(target: &Target) -> Self {
        let num_physical = target.num_qubits.unwrap_or_default() as usize;

        let one_q = (0..num_physical)
            .map(|physical| {
                let qargs = [PhysicalQubit::new(physical as u32)];
                exact_qarg_costs(target, &qargs)
            })
            .collect();

        let mut two_q = HashMap::new();
        if let Some(qargs_iter) = target.qargs() {
            for qargs in qargs_iter {
                let QargsRef::Concrete(qargs) = qargs.as_ref() else {
                    continue;
                };
                if qargs.len() != 2 {
                    continue;
                }
                if let Some(costs) = exact_qarg_costs(target, qargs) {
                    two_q.insert((qargs[0].index(), qargs[1].index()), costs);
                }
            }
        }

        Self { one_q, two_q }
    }

    fn score(&self, interactions: &CompactInteractions, mapping: &[usize]) -> Option<f64> {
        let mut score = 0.0;

        for (virtual_qubit, interaction) in &interactions.one_q {
            let physical = *mapping.get(*virtual_qubit)?;
            let costs = self.one_q.get(physical)?.as_ref()?;
            for (name, count) in &interaction.by_name {
                score += *costs.get(name)? * *count as f64;
            }
        }

        for ((left, right), interaction) in &interactions.two_q {
            let physical_left = *mapping.get(*left)?;
            let physical_right = *mapping.get(*right)?;
            let costs = self.two_q.get(&(physical_left, physical_right))?;
            for (name, count) in &interaction.by_name {
                score += *costs.get(name)? * *count as f64;
            }
        }

        Some(score)
    }
}

fn complete_mapping(
    partial: &HashMap<usize, usize>,
    num_virtual: usize,
    num_physical: usize,
    prefer_identity: bool,
) -> Option<Vec<usize>> {
    if num_virtual > num_physical {
        return None;
    }

    let mut mapping = vec![usize::MAX; num_virtual];
    let mut used = vec![false; num_physical];

    for (&virtual_qubit, &physical_qubit) in partial {
        if virtual_qubit >= num_virtual
            || physical_qubit >= num_physical
            || used[physical_qubit]
        {
            return None;
        }
        mapping[virtual_qubit] = physical_qubit;
        used[physical_qubit] = true;
    }

    let mut first_free = 0;
    for virtual_qubit in 0..num_virtual {
        if mapping[virtual_qubit] != usize::MAX {
            continue;
        }

        let physical_qubit =
            if prefer_identity && virtual_qubit < num_physical && !used[virtual_qubit] {
                virtual_qubit
            } else {
                while first_free < num_physical && used[first_free] {
                    first_free += 1;
                }
                if first_free == num_physical {
                    return None;
                }
                first_free
            };

        mapping[virtual_qubit] = physical_qubit;
        used[physical_qubit] = true;
    }

    Some(mapping)
}

fn mapping_as_hash_map(mapping: &[usize]) -> HashMap<usize, usize> {
    mapping.iter().copied().enumerate().collect()
}

#[pyfunction(name = "score_mappings")]
#[pyo3(signature = (dag, target, mappings, *, strict_direction=false, prefer_identity=true))]
fn py_score_mappings(
    dag: &PyDAGCircuit,
    target: &Target,
    mappings: Vec<HashMap<usize, usize>>,
    strict_direction: bool,
    prefer_identity: bool,
) -> PyResult<(Option<HashMap<usize, usize>>, f64, f64)> {
    let dag = dag.try_read()?;
    let interactions = CompactInteractions::from_dag(&dag, strict_direction)?;

    let num_virtual = dag.num_qubits();
    let num_physical = target.num_qubits.unwrap_or_default() as usize;

    let baseline_mapping = if num_virtual <= num_physical {
        Some((0..num_virtual).collect::<Vec<_>>())
    } else {
        None
    };

    let mut best_mapping = None;

    let (baseline_score, best_score) = if strict_direction {
        let costs = ExactTargetCosts::new(target);
        let baseline_score = baseline_mapping
            .as_deref()
            .and_then(|mapping| costs.score(&interactions, mapping))
            .unwrap_or(f64::INFINITY);
        let mut best_score = baseline_score;

        for partial in &mappings {
            let Some(candidate) =
                complete_mapping(partial, num_virtual, num_physical, prefer_identity)
            else {
                continue;
            };
            let Some(score) = costs.score(&interactions, &candidate) else {
                continue;
            };
            if score + SCORE_EPSILON < best_score {
                best_score = score;
                best_mapping = Some(candidate);
            }
        }

        (baseline_score, best_score)
    } else {
        let costs = AverageTargetCosts::new(target);
        let baseline_score = baseline_mapping
            .as_deref()
            .and_then(|mapping| costs.score(&interactions, mapping))
            .unwrap_or(f64::INFINITY);
        let mut best_score = baseline_score;

        for partial in &mappings {
            let Some(candidate) =
                complete_mapping(partial, num_virtual, num_physical, prefer_identity)
            else {
                continue;
            };
            let Some(score) = costs.score(&interactions, &candidate) else {
                continue;
            };
            if score + SCORE_EPSILON < best_score {
                best_score = score;
                best_mapping = Some(candidate);
            }
        }

        (baseline_score, best_score)
    };

    let best_mapping = best_mapping.map(|mapping| mapping_as_hash_map(&mapping));
    Ok((best_mapping, baseline_score, best_score))
}

pub fn glasgow_score_mod(m: &Bound<PyModule>) -> PyResult<()> {
    m.add_wrapped(wrap_pyfunction!(py_score_mappings))?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn complete_mapping_prefers_identity_then_lowest_free() {
        let partial = HashMap::from([(0, 3), (2, 2)]);
        let mapping = complete_mapping(&partial, 4, 5, true).unwrap();
        assert_eq!(mapping, vec![3, 1, 2, 0]);
    }

    #[test]
    fn compact_average_scoring_multiplies_interaction_counts() {
        let mut interactions = CompactInteractions::default();

        let mut one_q = InteractionWeight::default();
        one_q.total = 10;
        interactions.one_q.insert(0, one_q);

        let mut two_q = InteractionWeight::default();
        two_q.total = 20;
        interactions.two_q.insert((0, 1), two_q);

        let costs = AverageTargetCosts {
            one_q: vec![Some(0.5), Some(0.25)],
            two_q: HashMap::from([((0, 1), 0.75)]),
        };

        let score = costs.score(&interactions, &[0, 1]).unwrap();
        assert_eq!(score, 10.0 * 0.5 + 20.0 * 0.75);
    }
}
