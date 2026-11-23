#include "glasgow.h"

#include <gss/formats/input_graph.hh>
#include <gss/homomorphism.hh>
#include <gss/restarts.hh>
#include <gss/timeout.hh>

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <limits>
#include <memory>
#include <optional>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

using std::size_t;

namespace
{
    struct ErrorSample
    {
        std::string operation_name;
        double error;
    };

    struct TargetData
    {
        uint32_t num_qubits = 0;

        // Average raw error and -ln(1-error) for [q].  one_present mirrors
        // Qiskit's ErrorMap membership; the free-qubit assignment treats a
        // missing 1q entry as infinitely bad, while the coupled search treats
        // a missing entry as zero cost, matching vf2_layout.rs.
        std::vector<bool> one_present;
        std::vector<double> one_raw_error;
        std::vector<double> one_cost;

        // Directed [q0, q1] support/costs after Qiskit's non-strict
        // directionality relaxation.  structural_edge records the original
        // physical connectivity irrespective of direction.
        std::vector<bool> two_present;
        std::vector<double> two_cost;
        std::vector<bool> structural_edge;
    };

    struct InteractionData
    {
        uint32_t num_qubits = 0;
        std::vector<uint64_t> one_count;
        std::vector<uint64_t> two_count;
        std::vector<bool> coupled;
    };

    auto matrix_index(uint32_t n, uint32_t left, uint32_t right) -> size_t
    {
        return static_cast<size_t>(left) * n + right;
    }

    auto neg_log_fidelity(double error) -> double
    {
        if (std::isnan(error) || error <= 0.0)
            return 0.0;
        if (error >= 1.0)
            return std::numeric_limits<double>::max();
        return -std::log1p(-error);
    }

    auto average_samples(std::vector<ErrorSample> & samples) -> double
    {
        std::sort(samples.begin(), samples.end(), [](const auto & left, const auto & right) {
            return left.operation_name < right.operation_name;
        });

        double total = 0.0;
        for (const auto & sample : samples)
            total += sample.error;
        return samples.empty() ? 0.0 : total / static_cast<double>(samples.size());
    }

    auto build_target_data(QkTarget * target, TargetData & out, bool strict_direction) -> bool
    {
        const uint32_t n = qk_target_num_qubits(target);
        out.num_qubits = n;
        out.one_present.assign(n, false);
        out.one_raw_error.assign(n, 0.0);
        out.one_cost.assign(n, 0.0);
        out.two_present.assign(static_cast<size_t>(n) * n, false);
        out.two_cost.assign(static_cast<size_t>(n) * n, 0.0);
        out.structural_edge.assign(static_cast<size_t>(n) * n, false);

        std::vector<std::vector<ErrorSample>> one_samples(n);
        std::vector<std::vector<ErrorSample>> two_samples(static_cast<size_t>(n) * n);
        bool target_without_errors = true;

        const size_t num_operations = qk_target_num_instructions(target);
        for (size_t op_index = 0; op_index < num_operations; ++op_index) {
            QkTargetOp operation{};
            qk_target_op_get(target, op_index, &operation);
            const std::string operation_name = operation.name ? operation.name : "";

            const bool relevant = operation.num_qubits == 1 || operation.num_qubits == 2;
            if (! relevant) {
                qk_target_op_clear(&operation);
                continue;
            }

            if (operation.num_qubits == 2
                && qk_target_op_qargs_index(target, op_index, nullptr) != SIZE_MAX) {
                qk_target_op_clear(&operation);
                return false;
            }

            const size_t num_properties = qk_target_op_num_properties(target, op_index);
            for (size_t property_index = 0; property_index < num_properties; ++property_index) {
                uint32_t * qargs = nullptr;
                uint32_t qargs_len = 0;
                qk_target_op_qargs(target, op_index, property_index, &qargs, &qargs_len);

                // This is the same unsupported case as Qiskit's
                // build_average_error_map(): a global 2q operation makes a
                // finite coupling graph meaningless.
                if (! qargs) {
                    if (operation.num_qubits == 2) {
                        qk_target_op_clear(&operation);
                        return false;
                    }
                    continue;
                }
                if (qargs_len != operation.num_qubits)
                    continue;

                QkInstructionProperties properties{};
                qk_target_op_props(target, op_index, property_index, &properties);
                const double error = std::isnan(properties.error) ? 0.0 : properties.error;
                if (! std::isnan(properties.error))
                    target_without_errors = false;

                if (qargs_len == 1) {
                    const uint32_t q = qargs[0];
                    if (q < n)
                        one_samples[q].push_back({operation_name, error});
                }
                else {
                    const uint32_t left = qargs[0];
                    const uint32_t right = qargs[1];
                    if (left >= n || right >= n)
                        continue;
                    const size_t index = matrix_index(n, left, right);
                    two_samples[index].push_back({operation_name, error});
                    out.structural_edge[index] = true;
                }
            }

            qk_target_op_clear(&operation);
        }

        if (target_without_errors) {
            // Reproduce the legacy VF2 fallback: penalise high-degree physical
            // qubits when the Target reports no calibration errors at all.
            std::vector<unsigned> degree(n, 0);
            for (uint32_t left = 0; left < n; ++left) {
                for (uint32_t right = left + 1; right < n; ++right) {
                    if (out.structural_edge[matrix_index(n, left, right)]
                        || out.structural_edge[matrix_index(n, right, left)]) {
                        ++degree[left];
                        ++degree[right];
                    }
                }
            }
            for (uint32_t q = 0; q < n; ++q) {
                out.one_present[q] = true;
                out.one_raw_error[q] = n == 0 ? 0.0 : static_cast<double>(degree[q]) / n;
                out.one_cost[q] = neg_log_fidelity(out.one_raw_error[q]);
            }
            for (uint32_t left = 0; left < n; ++left) {
                for (uint32_t right = 0; right < n; ++right) {
                    const size_t index = matrix_index(n, left, right);
                    if (! out.structural_edge[index])
                        continue;
                    const double raw = 0.5 * (out.one_raw_error[left] + out.one_raw_error[right]);
                    out.two_present[index] = true;
                    out.two_cost[index] = neg_log_fidelity(raw);
                }
            }
        }
        else {
            for (uint32_t q = 0; q < n; ++q) {
                if (one_samples[q].empty())
                    continue;
                out.one_present[q] = true;
                out.one_raw_error[q] = average_samples(one_samples[q]);
                out.one_cost[q] = neg_log_fidelity(out.one_raw_error[q]);
            }
            for (uint32_t left = 0; left < n; ++left) {
                for (uint32_t right = 0; right < n; ++right) {
                    const size_t index = matrix_index(n, left, right);
                    if (! out.structural_edge[index])
                        continue;
                    // A concrete qarg can exist with no reported InstructionProperties;
                    // Qiskit treats the absent error as zero.
                    const double raw = two_samples[index].empty()
                        ? 0.0
                        : average_samples(two_samples[index]);
                    out.two_present[index] = true;
                    out.two_cost[index] = neg_log_fidelity(raw);
                }
            }
        }

        if (! strict_direction) {
            // Match Qiskit's non-strict average path: only synthesize a
            // reverse edge when it is absent; if both directions exist,
            // retain their distinct costs.
            for (uint32_t left = 0; left < n; ++left) {
                for (uint32_t right = left + 1; right < n; ++right) {
                    const size_t lr = matrix_index(n, left, right);
                    const size_t rl = matrix_index(n, right, left);
                    if (out.two_present[lr] && ! out.two_present[rl]) {
                        out.two_present[rl] = true;
                        out.two_cost[rl] = out.two_cost[lr];
                    }
                    else if (out.two_present[rl] && ! out.two_present[lr]) {
                        out.two_present[lr] = true;
                        out.two_cost[lr] = out.two_cost[rl];
                    }
                }
            }
        }

        return true;
    }

    auto build_interactions(QkDag * dag, InteractionData & out) -> bool
    {
        const uint32_t n = qk_dag_num_qubits(dag);
        out.num_qubits = n;
        out.one_count.assign(n, 0);
        out.two_count.assign(static_cast<size_t>(n) * n, 0);
        out.coupled.assign(n, false);

        const size_t num_ops = qk_dag_num_op_nodes(dag);
        std::vector<uint32_t> nodes(num_ops);
        if (num_ops > 0)
            qk_dag_topological_op_nodes(dag, nodes.data());

        for (uint32_t node : nodes) {
            const QkOperationKind kind = qk_dag_op_node_kind(dag, node);
            if (kind == QkOperationKind_Barrier)
                continue;
            if (kind == QkOperationKind_ControlFlow)
                return false;

            const uint32_t num_qubits = qk_dag_op_node_num_qubits(dag, node);
            const uint32_t * qubits = qk_dag_op_node_qubits(dag, node);
            if (num_qubits == 0)
                continue;
            if (num_qubits == 1) {
                if (qubits[0] >= n)
                    return false;
                ++out.one_count[qubits[0]];
            }
            else if (num_qubits == 2) {
                if (qubits[0] >= n || qubits[1] >= n)
                    return false;
                ++out.two_count[matrix_index(n, qubits[0], qubits[1])];
                out.coupled[qubits[0]] = true;
                out.coupled[qubits[1]] = true;
            }
            else {
                return false;
            }
        }

        return true;
    }

    auto coupled_score_for_mapping(
        const InteractionData & interactions,
        const TargetData & target,
        const std::vector<uint32_t> & pattern_to_virtual,
        const std::vector<uint32_t> & virtual_to_physical) -> std::optional<double>
    {
        double score = 0.0;
        const uint32_t vn = interactions.num_qubits;
        const uint32_t pn = target.num_qubits;

        for (uint32_t virtual_qubit : pattern_to_virtual) {
            if (virtual_qubit >= virtual_to_physical.size())
                return std::nullopt;
            const uint32_t physical = virtual_to_physical[virtual_qubit];
            if (physical >= pn)
                return std::nullopt;
            score += static_cast<double>(interactions.one_count[virtual_qubit])
                * target.one_cost[physical];
        }

        for (size_t i = 0; i < pattern_to_virtual.size(); ++i) {
            const uint32_t left = pattern_to_virtual[i];
            const uint32_t physical_left = virtual_to_physical[left];
            for (size_t j = i + 1; j < pattern_to_virtual.size(); ++j) {
                const uint32_t right = pattern_to_virtual[j];
                const uint32_t physical_right = virtual_to_physical[right];
                const uint64_t lr_count = interactions.two_count[matrix_index(vn, left, right)];
                const uint64_t rl_count = interactions.two_count[matrix_index(vn, right, left)];
                if (lr_count == 0 && rl_count == 0)
                    continue;
                const size_t physical_lr = matrix_index(pn, physical_left, physical_right);
                const size_t physical_rl = matrix_index(pn, physical_right, physical_left);
                if (lr_count > 0 && ! target.two_present[physical_lr])
                    return std::nullopt;
                if (rl_count > 0 && ! target.two_present[physical_rl])
                    return std::nullopt;
                score += static_cast<double>(lr_count) * target.two_cost[physical_lr];
                score += static_cast<double>(rl_count) * target.two_cost[physical_rl];
            }
        }

        return score;
    }

    auto map_free_qubits(
        const InteractionData & interactions,
        const TargetData & target,
        std::vector<uint32_t> & virtual_to_physical) -> bool
    {
        const uint32_t vn = interactions.num_qubits;
        const uint32_t pn = target.num_qubits;
        if (vn > pn)
            return false;

        std::vector<bool> used_physical(pn, false);
        for (uint32_t virtual_qubit = 0; virtual_qubit < vn; ++virtual_qubit) {
            if (interactions.coupled[virtual_qubit]) {
                const uint32_t physical = virtual_to_physical[virtual_qubit];
                if (physical >= pn || used_physical[physical])
                    return false;
                used_physical[physical] = true;
            }
        }

        std::vector<uint32_t> free_physical;
        for (uint32_t physical = 0; physical < pn; ++physical) {
            if (! used_physical[physical])
                free_physical.push_back(physical);
        }
        std::stable_sort(free_physical.begin(), free_physical.end(), [&](uint32_t a, uint32_t b) {
            const double score_a = target.one_present[a]
                ? target.one_raw_error[a]
                : std::numeric_limits<double>::infinity();
            const double score_b = target.one_present[b]
                ? target.one_raw_error[b]
                : std::numeric_limits<double>::infinity();
            if (score_a != score_b)
                return score_a < score_b;
            return a < b;
        });

        std::vector<uint32_t> uncoupled;
        std::vector<uint32_t> idle;
        for (uint32_t virtual_qubit = 0; virtual_qubit < vn; ++virtual_qubit) {
            if (interactions.coupled[virtual_qubit])
                continue;
            if (interactions.one_count[virtual_qubit] > 0)
                uncoupled.push_back(virtual_qubit);
            else
                idle.push_back(virtual_qubit);
        }
        std::stable_sort(uncoupled.begin(), uncoupled.end(), [&](uint32_t a, uint32_t b) {
            if (interactions.one_count[a] != interactions.one_count[b])
                return interactions.one_count[a] > interactions.one_count[b];
            return a < b;
        });

        size_t free_index = 0;
        for (uint32_t virtual_qubit : uncoupled) {
            if (free_index >= free_physical.size())
                return false;
            virtual_to_physical[virtual_qubit] = free_physical[free_index++];
        }
        for (uint32_t virtual_qubit : idle) {
            if (free_index >= free_physical.size())
                return false;
            virtual_to_physical[virtual_qubit] = free_physical[free_index++];
        }

        return true;
    }

    auto solve_mapping(QkDag * dag, QkTarget * target_handle, subgraph_vec mapping, bool post_layout, bool strict_direction) -> bool
    {
        InteractionData interactions;
        TargetData target;
        if (! build_interactions(dag, interactions) || ! build_target_data(target_handle, target, strict_direction))
            return false;
        if (interactions.num_qubits > target.num_qubits)
            return false;

        std::vector<uint32_t> pattern_to_virtual;
        pattern_to_virtual.reserve(interactions.num_qubits);
        for (uint32_t virtual_qubit = 0; virtual_qubit < interactions.num_qubits; ++virtual_qubit) {
            if (interactions.coupled[virtual_qubit])
                pattern_to_virtual.push_back(virtual_qubit);
        }

        std::vector<uint32_t> virtual_to_physical(
            interactions.num_qubits, std::numeric_limits<uint32_t>::max());

        if (! pattern_to_virtual.empty()) {
            InputGraph pattern_graph(
                static_cast<int>(pattern_to_virtual.size()), false, false, strict_direction);
            InputGraph target_graph(
                static_cast<int>(target.num_qubits), false, false, strict_direction);

            std::vector<int> virtual_to_pattern(interactions.num_qubits, -1);
            for (size_t pattern_vertex = 0; pattern_vertex < pattern_to_virtual.size(); ++pattern_vertex)
                virtual_to_pattern[pattern_to_virtual[pattern_vertex]] = static_cast<int>(pattern_vertex);

            if (strict_direction) {
                for (uint32_t left = 0; left < interactions.num_qubits; ++left) {
                    if (! interactions.coupled[left])
                        continue;
                    for (uint32_t right = 0; right < interactions.num_qubits; ++right) {
                        if (left == right || ! interactions.coupled[right])
                            continue;
                        if (interactions.two_count[
                                matrix_index(interactions.num_qubits, left, right)] == 0)
                            continue;
                        pattern_graph.add_directed_edge(
                            virtual_to_pattern[left], virtual_to_pattern[right], "");
                    }
                }

                for (uint32_t left = 0; left < target.num_qubits; ++left) {
                    for (uint32_t right = 0; right < target.num_qubits; ++right) {
                        if (left == right)
                            continue;
                        if (target.structural_edge[
                                matrix_index(target.num_qubits, left, right)])
                            target_graph.add_directed_edge(
                                static_cast<int>(left), static_cast<int>(right), "");
                    }
                }
            }
            else {
                for (uint32_t left = 0; left < interactions.num_qubits; ++left) {
                    if (! interactions.coupled[left])
                        continue;
                    for (uint32_t right = left + 1; right < interactions.num_qubits; ++right) {
                        if (! interactions.coupled[right])
                            continue;
                        if (interactions.two_count[
                                matrix_index(interactions.num_qubits, left, right)] == 0
                            && interactions.two_count[
                                matrix_index(interactions.num_qubits, right, left)] == 0)
                            continue;
                        pattern_graph.add_edge(
                            virtual_to_pattern[left], virtual_to_pattern[right]);
                    }
                }

                for (uint32_t left = 0; left < target.num_qubits; ++left) {
                    for (uint32_t right = left + 1; right < target.num_qubits; ++right) {
                        if (target.structural_edge[
                                matrix_index(target.num_qubits, left, right)]
                            || target.structural_edge[
                                matrix_index(target.num_qubits, right, left)])
                            target_graph.add_edge(
                                static_cast<int>(left), static_cast<int>(right));
                    }
                }
            }

            gss::HomomorphismParams params;
            params.timeout = std::make_shared<gss::Timeout>(std::chrono::seconds{0});
            params.restarts_schedule = std::make_unique<gss::NoRestartsSchedule>();
            params.n_threads = 1;
            params.injectivity = gss::Injectivity::Injective;
            params.induced = false;

            gss::HomomorphismObjective objective;
            objective.order_values_by_cost = true;
            objective.vertex_cost = [&](unsigned pattern_vertex, unsigned physical) {
                const uint32_t virtual_qubit = pattern_to_virtual.at(pattern_vertex);
                return static_cast<double>(interactions.one_count[virtual_qubit])
                    * target.one_cost.at(physical);
            };
            objective.pair_cost = [&](unsigned p, unsigned q, unsigned physical_p, unsigned physical_q) {
                if (p == q)
                    return 0.0;
                const uint32_t virtual_p = pattern_to_virtual.at(p);
                const uint32_t virtual_q = pattern_to_virtual.at(q);
                const uint64_t pq_count = interactions.two_count[
                    matrix_index(interactions.num_qubits, virtual_p, virtual_q)];
                const uint64_t qp_count = interactions.two_count[
                    matrix_index(interactions.num_qubits, virtual_q, virtual_p)];
                if (pq_count == 0 && qp_count == 0)
                    return 0.0;

                const size_t pq = matrix_index(target.num_qubits, physical_p, physical_q);
                const size_t qp = matrix_index(target.num_qubits, physical_q, physical_p);
                // Structural propagation guarantees one physical direction exists;
                // build_target_data() has already loosened the absent reverse direction.
                if (pq_count > 0 && ! target.two_present[pq])
                    return std::numeric_limits<double>::max();
                if (qp_count > 0 && ! target.two_present[qp])
                    return std::numeric_limits<double>::max();
                return static_cast<double>(pq_count) * target.two_cost[pq]
                    + static_cast<double>(qp_count) * target.two_cost[qp];
            };

            if (post_layout) {
                std::vector<uint32_t> identity(interactions.num_qubits);
                for (uint32_t q = 0; q < interactions.num_qubits; ++q)
                    identity[q] = q;
                const auto incumbent = coupled_score_for_mapping(
                    interactions, target, pattern_to_virtual, identity);
                if (! incumbent)
                    return false;
                objective.initial_upper_bound = *incumbent;
            }

            params.objective = std::move(objective);
            const auto result = gss::solve_homomorphism_problem(pattern_graph, target_graph, params);
            if (result.mapping.empty())
                return false;

            for (const auto & [pattern_vertex, physical] : result.mapping) {
                if (pattern_vertex < 0 || static_cast<size_t>(pattern_vertex) >= pattern_to_virtual.size()
                    || physical < 0 || static_cast<uint32_t>(physical) >= target.num_qubits)
                    return false;
                virtual_to_physical[pattern_to_virtual[pattern_vertex]] = static_cast<uint32_t>(physical);
            }
        }
        else if (post_layout) {
            // Qiskit's VF2PostLayout has no coupled score to improve in this case.
            return false;
        }

        if (! map_free_qubits(interactions, target, virtual_to_physical))
            return false;

        for (uint32_t virtual_qubit = 0; virtual_qubit < interactions.num_qubits; ++virtual_qubit)
            mapping[virtual_qubit] = virtual_to_physical[virtual_qubit];
        return true;
    }
}

int glasgow_qiskit_import(void)
{
    return qk_import();
}

bool glasgow_mapping(QkDag * dag, QkTarget * target, subgraph_vec mapping)
{
    try {
        return solve_mapping(dag, target, mapping, false, false);
    }
    catch (const std::exception & error) {
        PyErr_SetString(PyExc_RuntimeError, error.what());
        return false;
    }
}

bool glasgow_mapping_post(
    QkDag * dag,
    QkTarget * target,
    subgraph_vec mapping,
    bool strict_direction)
{
    try {
        return solve_mapping(dag, target, mapping, true, strict_direction);
    }
    catch (const std::exception & error) {
        PyErr_SetString(PyExc_RuntimeError, error.what());
        return false;
    }
}
