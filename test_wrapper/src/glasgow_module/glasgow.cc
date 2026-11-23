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
#include <tuple>
#include <unordered_map>
#include <utility>
#include <vector>

using std::size_t;

namespace
{
    using InstructionCounts = std::unordered_map<std::string, uint64_t>;
    using InstructionCosts = std::unordered_map<std::string, double>;

    struct ErrorSample
    {
        std::string operation_name;
        double error;
    };

    struct AverageTargetData
    {
        uint32_t num_qubits = 0;
        std::vector<bool> one_present;
        std::vector<double> one_raw_error;
        std::vector<double> one_cost;
        std::vector<bool> two_present;
        std::vector<double> two_cost;
        std::vector<bool> structural_edge;
    };

    struct ExactTargetData
    {
        uint32_t num_qubits = 0;
        std::vector<InstructionCosts> one_costs;
        std::vector<InstructionCosts> two_costs;
        std::vector<bool> structural_edge;
        bool has_concrete_qargs = false;
    };

    struct AverageInteractionData
    {
        uint32_t num_qubits = 0;
        std::vector<uint64_t> one_count;
        std::vector<uint64_t> two_count;
        std::vector<bool> coupled;
    };

    struct ExactInteractionData
    {
        uint32_t num_qubits = 0;
        std::vector<InstructionCounts> one_uses;
        std::vector<InstructionCounts> two_uses;
        std::vector<bool> coupled;
    };

    auto matrix_index(uint32_t n, uint32_t left, uint32_t right) -> size_t
    {
        return static_cast<size_t>(left) * n + right;
    }

    auto target_operation_name(QkTarget * target, size_t op_index) -> std::string
    {
        char * raw_name = qk_target_op_name(target, op_index);
        if (! raw_name)
            return {};
        std::string name(raw_name);
        qk_str_free(raw_name);
        return name;
    }

    auto exact_interaction_width(
        const ExactInteractionData & interactions,
        const std::string & operation_name) -> std::optional<uint32_t>
    {
        bool used_as_one_qubit = false;
        bool used_as_two_qubit = false;

        for (const auto & uses : interactions.one_uses) {
            if (uses.find(operation_name) != uses.end()) {
                used_as_one_qubit = true;
                break;
            }
        }
        for (const auto & uses : interactions.two_uses) {
            if (uses.find(operation_name) != uses.end()) {
                used_as_two_qubit = true;
                break;
            }
        }

        // A name used at both widths is effectively variadic for our purposes.  Qiskit's
        // exact global-operation handling skips TargetOperation::Variadic, so do the same.
        if (used_as_one_qubit == used_as_two_qubit)
            return std::nullopt;
        return used_as_one_qubit ? std::optional<uint32_t>{1u}
                                 : std::optional<uint32_t>{2u};
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

    auto build_average_target_data(QkTarget * target, AverageTargetData & out, bool strict_direction) -> bool
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
        bool has_concrete_qargs = false;

        const size_t num_operations = qk_target_num_instructions(target);
        for (size_t op_index = 0; op_index < num_operations; ++op_index) {
            // Do not use qk_target_op_get() here.  In Qiskit 2.5.x that C API function
            // aborts the process for Target operations whose Python/Rust operation type is
            // not one of its small set of materializable C operation kinds (notably dynamic
            // control-flow operations present on IBM hardware Targets).  For VF2 scoring we
            // only need the operation name, qargs and properties, all of which have dedicated
            // C API accessors that work without materializing the operation object.
            const std::string operation_name = target_operation_name(target, op_index);
            const size_t num_properties = qk_target_op_num_properties(target, op_index);
            bool saw_global = false;
            bool saw_concrete_two_qubit = false;

            for (size_t property_index = 0; property_index < num_properties; ++property_index) {
                uint32_t * qargs = nullptr;
                uint32_t qargs_len = 0;
                qk_target_op_qargs(target, op_index, property_index, &qargs, &qargs_len);

                if (! qargs) {
                    saw_global = true;
                    continue;
                }
                if (qargs_len != 1 && qargs_len != 2)
                    continue;

                has_concrete_qargs = true;
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
                    saw_concrete_two_qubit = true;
                    const uint32_t left = qargs[0];
                    const uint32_t right = qargs[1];
                    if (left >= n || right >= n)
                        continue;
                    const size_t index = matrix_index(n, left, right);
                    two_samples[index].push_back({operation_name, error});
                    out.structural_edge[index] = true;
                }
            }

            // Match Qiskit's average path for global 2q operations whenever the operation also
            // exposes concrete 2q qargs: the average coupling graph is not meaningful, so bail.
            // Global-only operations whose arity is not exposed by the C iteration API are
            // ignored.  On IBM hardware Targets these are dynamic-control-flow operations, which
            // are irrelevant to the 1q/2q VF2 interaction graph.
            if (saw_global && saw_concrete_two_qubit)
                return false;
        }

        // build_average_coupling_map returns None when Target::num_qargs() is zero.
        if (! has_concrete_qargs)
            return false;

        if (target_without_errors) {
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
                    const double raw = two_samples[index].empty()
                        ? 0.0
                        : average_samples(two_samples[index]);
                    out.two_present[index] = true;
                    out.two_cost[index] = neg_log_fidelity(raw);
                }
            }
        }

        if (! strict_direction) {
            // Qiskit keeps both graphs directed and adds a reversed target edge only when one is
            // absent. If both directions exist their independent calibration costs are retained.
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

    auto build_exact_target_data(
        QkTarget * target,
        const ExactInteractionData & interactions,
        ExactTargetData & out) -> bool
    {
        const uint32_t n = qk_target_num_qubits(target);
        out.num_qubits = n;
        out.one_costs.assign(n, {});
        out.two_costs.assign(static_cast<size_t>(n) * n, {});
        out.structural_edge.assign(static_cast<size_t>(n) * n, false);

        std::vector<std::pair<std::string, uint32_t>> global_operations;
        const size_t num_operations = qk_target_num_instructions(target);
        for (size_t op_index = 0; op_index < num_operations; ++op_index) {
            const std::string operation_name = target_operation_name(target, op_index);
            const size_t num_properties = qk_target_op_num_properties(target, op_index);
            bool saw_global = false;
            bool saw_one_qubit = false;
            bool saw_two_qubit = false;

            for (size_t property_index = 0; property_index < num_properties; ++property_index) {
                uint32_t * qargs = nullptr;
                uint32_t qargs_len = 0;
                qk_target_op_qargs(target, op_index, property_index, &qargs, &qargs_len);
                if (! qargs) {
                    saw_global = true;
                    continue;
                }
                if (qargs_len != 1 && qargs_len != 2)
                    continue;

                out.has_concrete_qargs = true;
                QkInstructionProperties properties{};
                qk_target_op_props(target, op_index, property_index, &properties);
                const double cost = neg_log_fidelity(properties.error);

                if (qargs_len == 1) {
                    saw_one_qubit = true;
                    if (qargs[0] < n)
                        out.one_costs[qargs[0]][operation_name] = cost;
                }
                else if (qargs[0] < n && qargs[1] < n) {
                    saw_two_qubit = true;
                    const size_t index = matrix_index(n, qargs[0], qargs[1]);
                    out.two_costs[index][operation_name] = cost;
                    out.structural_edge[index] = true;
                }
            }

            if (saw_global) {
                std::optional<uint32_t> width;
                if (saw_one_qubit != saw_two_qubit)
                    width = saw_one_qubit ? 1u : 2u;
                else if (! saw_one_qubit && ! saw_two_qubit)
                    width = exact_interaction_width(interactions, operation_name);

                // If both widths were observed, the operation is effectively variadic and Qiskit's
                // exact global handling skips it.  If neither width can be inferred and the circuit
                // does not use the operation, it is likewise irrelevant to this VF2 problem.
                if (width)
                    global_operations.emplace_back(operation_name, *width);
            }
        }

        // build_exact_coupling_map gives up when there are only global operations.
        if (! out.has_concrete_qargs)
            return false;

        // Match Qiskit's exact handling of globals: global 1q instructions are ideal on every
        // physical qubit; global 2q instructions are ideal only on links that already have a
        // concrete 2q instruction.
        for (const auto & [name, width] : global_operations) {
            if (width == 1) {
                for (auto & costs : out.one_costs)
                    costs[name] = 0.0;
            }
            else if (width == 2) {
                for (uint32_t left = 0; left < n; ++left) {
                    for (uint32_t right = 0; right < n; ++right) {
                        const size_t index = matrix_index(n, left, right);
                        if (out.structural_edge[index])
                            out.two_costs[index][name] = 0.0;
                    }
                }
            }
        }

        return true;
    }

    auto build_average_interactions(QkDag * dag, AverageInteractionData & out) -> bool
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
            else
                return false;
        }

        return true;
    }

    auto build_exact_interactions(QkDag * dag, ExactInteractionData & out) -> bool
    {
        const uint32_t n = qk_dag_num_qubits(dag);
        out.num_qubits = n;
        out.one_uses.assign(n, {});
        out.two_uses.assign(static_cast<size_t>(n) * n, {});
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

            QkCircuitInstruction instruction{};
            qk_dag_get_instruction(dag, node, &instruction);
            const std::string name = instruction.name ? instruction.name : "";
            const uint32_t num_qubits = instruction.num_qubits;

            bool valid = true;
            if (num_qubits == 0) {
                // Nothing to record.
            }
            else if (num_qubits == 1) {
                if (instruction.qubits[0] >= n)
                    valid = false;
                else
                    ++out.one_uses[instruction.qubits[0]][name];
            }
            else if (num_qubits == 2) {
                if (instruction.qubits[0] >= n || instruction.qubits[1] >= n)
                    valid = false;
                else {
                    ++out.two_uses[
                        matrix_index(n, instruction.qubits[0], instruction.qubits[1])][name];
                    out.coupled[instruction.qubits[0]] = true;
                    out.coupled[instruction.qubits[1]] = true;
                }
            }
            else
                valid = false;

            qk_circuit_instruction_clear(&instruction);
            if (! valid)
                return false;
        }

        return true;
    }

    auto average_score_for_mapping(
        const AverageInteractionData & interactions,
        const AverageTargetData & target,
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
                for (const auto [virtual_source, virtual_target, physical_source, physical_target] : {
                         std::tuple{left, right, physical_left, physical_right},
                         std::tuple{right, left, physical_right, physical_left}}) {
                    const uint64_t count = interactions.two_count[
                        matrix_index(vn, virtual_source, virtual_target)];
                    if (count == 0)
                        continue;
                    const size_t physical_index = matrix_index(pn, physical_source, physical_target);
                    if (! target.two_present[physical_index])
                        return std::nullopt;
                    score += static_cast<double>(count) * target.two_cost[physical_index];
                }
            }
        }
        return score;
    }

    auto exact_instruction_score(
        const InstructionCounts & uses,
        const InstructionCosts & costs) -> std::optional<double>
    {
        double score = 0.0;
        for (const auto & [name, count] : uses) {
            const auto found = costs.find(name);
            if (found == costs.end())
                return std::nullopt;
            score += static_cast<double>(count) * found->second;
        }
        return score;
    }

    auto exact_score_for_mapping(
        const ExactInteractionData & interactions,
        const ExactTargetData & target,
        const std::vector<uint32_t> & pattern_to_virtual,
        const std::vector<uint32_t> & virtual_to_physical) -> std::optional<double>
    {
        double score = 0.0;
        const uint32_t vn = interactions.num_qubits;
        const uint32_t pn = target.num_qubits;
        for (uint32_t virtual_qubit : pattern_to_virtual) {
            const uint32_t physical = virtual_to_physical.at(virtual_qubit);
            if (physical >= pn)
                return std::nullopt;
            const auto partial = exact_instruction_score(
                interactions.one_uses[virtual_qubit], target.one_costs[physical]);
            if (! partial)
                return std::nullopt;
            score += *partial;
        }

        for (size_t i = 0; i < pattern_to_virtual.size(); ++i) {
            const uint32_t left = pattern_to_virtual[i];
            const uint32_t physical_left = virtual_to_physical[left];
            for (size_t j = i + 1; j < pattern_to_virtual.size(); ++j) {
                const uint32_t right = pattern_to_virtual[j];
                const uint32_t physical_right = virtual_to_physical[right];
                for (const auto [virtual_source, virtual_target, physical_source, physical_target] : {
                         std::tuple{left, right, physical_left, physical_right},
                         std::tuple{right, left, physical_right, physical_left}}) {
                    const auto & uses = interactions.two_uses[
                        matrix_index(vn, virtual_source, virtual_target)];
                    if (uses.empty())
                        continue;
                    const auto partial = exact_instruction_score(
                        uses,
                        target.two_costs[matrix_index(pn, physical_source, physical_target)]);
                    if (! partial)
                        return std::nullopt;
                    score += *partial;
                }
            }
        }
        return score;
    }

    auto map_average_free_qubits(
        const AverageInteractionData & interactions,
        const AverageTargetData & target,
        std::vector<uint32_t> & virtual_to_physical) -> bool
    {
        const uint32_t vn = interactions.num_qubits;
        const uint32_t pn = target.num_qubits;
        if (vn > pn)
            return false;

        std::vector<bool> used_physical(pn, false);
        for (uint32_t virtual_qubit = 0; virtual_qubit < vn; ++virtual_qubit) {
            if (! interactions.coupled[virtual_qubit])
                continue;
            const uint32_t physical = virtual_to_physical[virtual_qubit];
            if (physical >= pn || used_physical[physical])
                return false;
            used_physical[physical] = true;
        }

        std::vector<uint32_t> free_physical;
        for (uint32_t physical = 0; physical < pn; ++physical)
            if (! used_physical[physical])
                free_physical.push_back(physical);
        std::stable_sort(free_physical.begin(), free_physical.end(), [&](uint32_t a, uint32_t b) {
            const double score_a = target.one_present[a]
                ? (std::isnan(target.one_raw_error[a]) ? 0.0 : target.one_raw_error[a])
                : std::numeric_limits<double>::infinity();
            const double score_b = target.one_present[b]
                ? (std::isnan(target.one_raw_error[b]) ? 0.0 : target.one_raw_error[b])
                : std::numeric_limits<double>::infinity();
            return score_a < score_b;
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
            return interactions.one_count[a] > interactions.one_count[b];
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

    auto map_exact_idle_qubits(
        const ExactInteractionData & interactions,
        uint32_t num_physical_qubits,
        std::vector<uint32_t> & virtual_to_physical) -> bool
    {
        if (interactions.num_qubits > num_physical_qubits)
            return false;
        std::vector<bool> used(num_physical_qubits, false);
        for (uint32_t virtual_qubit = 0; virtual_qubit < interactions.num_qubits; ++virtual_qubit) {
            if (interactions.one_uses[virtual_qubit].empty() && ! interactions.coupled[virtual_qubit])
                continue;
            const uint32_t physical = virtual_to_physical[virtual_qubit];
            if (physical >= num_physical_qubits || used[physical])
                return false;
            used[physical] = true;
        }

        uint32_t next_physical = 0;
        for (uint32_t virtual_qubit = 0; virtual_qubit < interactions.num_qubits; ++virtual_qubit) {
            if (! interactions.one_uses[virtual_qubit].empty() || interactions.coupled[virtual_qubit])
                continue;
            while (next_physical < num_physical_qubits && used[next_physical])
                ++next_physical;
            if (next_physical >= num_physical_qubits)
                return false;
            virtual_to_physical[virtual_qubit] = next_physical;
            used[next_physical] = true;
        }
        return true;
    }

    auto configure_objective_limits(
        gss::HomomorphismObjective & objective,
        int64_t first_call_limit,
        int64_t after_call_limit,
        int64_t max_trials,
        unsigned long long default_max_trials) -> void
    {
        if (first_call_limit >= 0)
            objective.first_solution_call_limit = static_cast<unsigned long long>(first_call_limit);
        if (after_call_limit >= 0)
            objective.after_solution_call_limit = static_cast<unsigned long long>(after_call_limit);
        if (max_trials >= 0)
            objective.max_solution_trials = static_cast<unsigned long long>(max_trials);
        else
            objective.max_solution_trials = default_max_trials;
    }

    auto solve_average_mapping(
        QkDag * dag,
        QkTarget * target_handle,
        subgraph_vec mapping,
        bool post_layout,
        bool strict_direction,
        int64_t first_call_limit,
        int64_t after_call_limit,
        int64_t max_trials) -> bool
    {
        AverageInteractionData interactions;
        AverageTargetData target;
        if (! build_average_interactions(dag, interactions)
            || ! build_average_target_data(target_handle, target, strict_direction))
            return false;
        if (interactions.num_qubits > target.num_qubits)
            return false;

        std::vector<uint32_t> pattern_to_virtual;
        for (uint32_t q = 0; q < interactions.num_qubits; ++q)
            if (interactions.coupled[q])
                pattern_to_virtual.push_back(q);

        std::vector<uint32_t> virtual_to_physical(
            interactions.num_qubits, std::numeric_limits<uint32_t>::max());

        if (! pattern_to_virtual.empty()) {
            // Qiskit's average VF2 graph remains directed even when strict_direction=False; only
            // the target directionality is loosened.
            InputGraph pattern_graph(static_cast<int>(pattern_to_virtual.size()), false, false, true);
            InputGraph target_graph(static_cast<int>(target.num_qubits), false, false, true);
            std::vector<int> virtual_to_pattern(interactions.num_qubits, -1);
            for (size_t p = 0; p < pattern_to_virtual.size(); ++p)
                virtual_to_pattern[pattern_to_virtual[p]] = static_cast<int>(p);

            unsigned long long pattern_edges = 0;
            for (uint32_t left = 0; left < interactions.num_qubits; ++left) {
                if (! interactions.coupled[left])
                    continue;
                for (uint32_t right = 0; right < interactions.num_qubits; ++right) {
                    if (left == right || ! interactions.coupled[right])
                        continue;
                    if (interactions.two_count[matrix_index(interactions.num_qubits, left, right)] == 0)
                        continue;
                    pattern_graph.add_directed_edge(
                        virtual_to_pattern[left], virtual_to_pattern[right], "");
                    ++pattern_edges;
                }
            }

            unsigned long long target_edges = 0;
            for (uint32_t left = 0; left < target.num_qubits; ++left) {
                for (uint32_t right = 0; right < target.num_qubits; ++right) {
                    if (left == right)
                        continue;
                    if (target.two_present[matrix_index(target.num_qubits, left, right)]) {
                        target_graph.add_directed_edge(static_cast<int>(left), static_cast<int>(right), "");
                        ++target_edges;
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
            objective.pair_allowed = [&](unsigned p, unsigned q, unsigned physical_p, unsigned physical_q) {
                if (p == q)
                    return true;
                const uint32_t virtual_p = pattern_to_virtual.at(p);
                const uint32_t virtual_q = pattern_to_virtual.at(q);
                for (const auto [vs, vt, ps, pt] : {
                         std::tuple{virtual_p, virtual_q, uint32_t(physical_p), uint32_t(physical_q)},
                         std::tuple{virtual_q, virtual_p, uint32_t(physical_q), uint32_t(physical_p)}}) {
                    if (interactions.two_count[matrix_index(interactions.num_qubits, vs, vt)] > 0
                        && ! target.two_present[matrix_index(target.num_qubits, ps, pt)])
                        return false;
                }
                return true;
            };
            objective.pair_cost = [&](unsigned p, unsigned q, unsigned physical_p, unsigned physical_q) {
                if (p == q)
                    return 0.0;
                const uint32_t virtual_p = pattern_to_virtual.at(p);
                const uint32_t virtual_q = pattern_to_virtual.at(q);
                double score = 0.0;
                for (const auto [vs, vt, ps, pt] : {
                         std::tuple{virtual_p, virtual_q, uint32_t(physical_p), uint32_t(physical_q)},
                         std::tuple{virtual_q, virtual_p, uint32_t(physical_q), uint32_t(physical_p)}}) {
                    const uint64_t count = interactions.two_count[
                        matrix_index(interactions.num_qubits, vs, vt)];
                    if (count > 0)
                        score += static_cast<double>(count)
                            * target.two_cost[matrix_index(target.num_qubits, ps, pt)];
                }
                return score;
            };

            configure_objective_limits(
                objective,
                first_call_limit,
                after_call_limit,
                max_trials,
                15 + std::max(pattern_edges, target_edges));

            if (post_layout) {
                std::vector<uint32_t> identity(interactions.num_qubits);
                for (uint32_t q = 0; q < interactions.num_qubits; ++q)
                    identity[q] = q;
                if (const auto incumbent = average_score_for_mapping(
                        interactions, target, pattern_to_virtual, identity))
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
        else if (post_layout)
            return false;

        if (! map_average_free_qubits(interactions, target, virtual_to_physical))
            return false;
        for (uint32_t q = 0; q < interactions.num_qubits; ++q)
            mapping[q] = virtual_to_physical[q];
        return true;
    }

    auto solve_exact_post_mapping(
        QkDag * dag,
        QkTarget * target_handle,
        subgraph_vec mapping,
        int64_t first_call_limit,
        int64_t after_call_limit,
        int64_t max_trials) -> bool
    {
        ExactInteractionData interactions;
        ExactTargetData target;
        if (! build_exact_interactions(dag, interactions)
            || ! build_exact_target_data(target_handle, interactions, target))
            return false;
        if (interactions.num_qubits > target.num_qubits)
            return false;

        std::vector<uint32_t> pattern_to_virtual;
        unsigned uncoupled_count = 0;
        for (uint32_t q = 0; q < interactions.num_qubits; ++q) {
            if (interactions.coupled[q])
                pattern_to_virtual.push_back(q);
            else if (! interactions.one_uses[q].empty()) {
                ++uncoupled_count;
                pattern_to_virtual.push_back(q);
            }
        }
        // This is the same deliberate bailout as vf2_layout_pass_exact().
        if (uncoupled_count > 1)
            return false;

        std::vector<uint32_t> virtual_to_physical(
            interactions.num_qubits, std::numeric_limits<uint32_t>::max());

        if (! pattern_to_virtual.empty()) {
            InputGraph pattern_graph(static_cast<int>(pattern_to_virtual.size()), false, false, true);
            InputGraph target_graph(static_cast<int>(target.num_qubits), false, false, true);
            std::vector<int> virtual_to_pattern(interactions.num_qubits, -1);
            for (size_t p = 0; p < pattern_to_virtual.size(); ++p)
                virtual_to_pattern[pattern_to_virtual[p]] = static_cast<int>(p);

            unsigned long long pattern_edges = 0;
            for (uint32_t left = 0; left < interactions.num_qubits; ++left) {
                if (virtual_to_pattern[left] < 0)
                    continue;
                for (uint32_t right = 0; right < interactions.num_qubits; ++right) {
                    if (left == right || virtual_to_pattern[right] < 0)
                        continue;
                    if (interactions.two_uses[matrix_index(interactions.num_qubits, left, right)].empty())
                        continue;
                    pattern_graph.add_directed_edge(
                        virtual_to_pattern[left], virtual_to_pattern[right], "");
                    ++pattern_edges;
                }
            }

            unsigned long long target_edges = 0;
            for (uint32_t left = 0; left < target.num_qubits; ++left) {
                for (uint32_t right = 0; right < target.num_qubits; ++right) {
                    if (left == right)
                        continue;
                    if (target.structural_edge[matrix_index(target.num_qubits, left, right)]) {
                        target_graph.add_directed_edge(static_cast<int>(left), static_cast<int>(right), "");
                        ++target_edges;
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
            objective.vertex_allowed = [&](unsigned p, unsigned physical) {
                const uint32_t virtual_q = pattern_to_virtual.at(p);
                return exact_instruction_score(
                    interactions.one_uses[virtual_q], target.one_costs.at(physical)).has_value();
            };
            objective.vertex_cost = [&](unsigned p, unsigned physical) {
                const uint32_t virtual_q = pattern_to_virtual.at(p);
                return exact_instruction_score(
                    interactions.one_uses[virtual_q], target.one_costs.at(physical)).value_or(0.0);
            };
            objective.pair_allowed = [&](unsigned p, unsigned q, unsigned physical_p, unsigned physical_q) {
                if (p == q)
                    return true;
                const uint32_t virtual_p = pattern_to_virtual.at(p);
                const uint32_t virtual_q = pattern_to_virtual.at(q);
                for (const auto [vs, vt, ps, pt] : {
                         std::tuple{virtual_p, virtual_q, uint32_t(physical_p), uint32_t(physical_q)},
                         std::tuple{virtual_q, virtual_p, uint32_t(physical_q), uint32_t(physical_p)}}) {
                    const auto & uses = interactions.two_uses[
                        matrix_index(interactions.num_qubits, vs, vt)];
                    if (! uses.empty()
                        && ! exact_instruction_score(
                            uses, target.two_costs[matrix_index(target.num_qubits, ps, pt)]))
                        return false;
                }
                return true;
            };
            objective.pair_cost = [&](unsigned p, unsigned q, unsigned physical_p, unsigned physical_q) {
                if (p == q)
                    return 0.0;
                const uint32_t virtual_p = pattern_to_virtual.at(p);
                const uint32_t virtual_q = pattern_to_virtual.at(q);
                double score = 0.0;
                for (const auto [vs, vt, ps, pt] : {
                         std::tuple{virtual_p, virtual_q, uint32_t(physical_p), uint32_t(physical_q)},
                         std::tuple{virtual_q, virtual_p, uint32_t(physical_q), uint32_t(physical_p)}}) {
                    const auto & uses = interactions.two_uses[
                        matrix_index(interactions.num_qubits, vs, vt)];
                    if (uses.empty())
                        continue;
                    score += exact_instruction_score(
                        uses, target.two_costs[matrix_index(target.num_qubits, ps, pt)]).value_or(0.0);
                }
                return score;
            };

            configure_objective_limits(
                objective,
                first_call_limit,
                after_call_limit,
                max_trials,
                15 + std::max(pattern_edges, target_edges));

            std::vector<uint32_t> identity(interactions.num_qubits);
            for (uint32_t q = 0; q < interactions.num_qubits; ++q)
                identity[q] = q;
            if (const auto incumbent = exact_score_for_mapping(
                    interactions, target, pattern_to_virtual, identity))
                objective.initial_upper_bound = *incumbent;

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
        else
            return false;

        if (! map_exact_idle_qubits(interactions, target.num_qubits, virtual_to_physical))
            return false;
        for (uint32_t q = 0; q < interactions.num_qubits; ++q)
            mapping[q] = virtual_to_physical[q];
        return true;
    }
}

int glasgow_qiskit_import(void)
{
    return qk_import();
}

bool glasgow_mapping(
    QkDag * dag,
    QkTarget * target,
    subgraph_vec mapping,
    bool strict_direction,
    int64_t first_call_limit,
    int64_t after_call_limit,
    int64_t max_trials)
{
    try {
        return solve_average_mapping(
            dag, target, mapping, false, strict_direction,
            first_call_limit, after_call_limit, max_trials);
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
    bool strict_direction,
    int64_t first_call_limit,
    int64_t after_call_limit,
    int64_t max_trials)
{
    try {
        if (strict_direction)
            return solve_exact_post_mapping(
                dag, target, mapping, first_call_limit, after_call_limit, max_trials);
        return solve_average_mapping(
            dag, target, mapping, true, false,
            first_call_limit, after_call_limit, max_trials);
    }
    catch (const std::exception & error) {
        PyErr_SetString(PyExc_RuntimeError, error.what());
        return false;
    }
}
