__author__ = 'marvinler'
# Copyright (C) 2017-2018 RTE and INRIA (France)
# Authors: Marvin Lerousseau <marvin.lerousseau@gmail.com>
# This file is under the LGPL-v3 license and is part of PyPowNet.
import os
import numpy as np
import copy
from oct2py import octave
from oct2py.utils import Oct2PyError
from pypownet import ARTIFICIAL_NODE_STARTING_STRING
from pypownet.chronic import TimestepEntries
import pypownet.environment
import math


class DivergingLoadflowException(Exception):
    def __init__(self, last_observation, *args):
        super(DivergingLoadflowException, self).__init__(last_observation, *args)
        self.last_observation = last_observation
        self.text = args[0]


class GridNotConnexeException(Exception):
    def __init__(self, last_observation, *args):
        super(GridNotConnexeException, self).__init__(last_observation, *args)
        self.last_observation = last_observation


def compute_flows_a(active, reactive, voltage, are_lines_on):
    # TODO: verify that the formula is correct
    assert len(active) == len(reactive) == len(voltage) == len(are_lines_on)
    flows_a = np.zeros(len(active))
    for i, (p, q, v, is_on) in enumerate(zip(active, reactive, voltage, are_lines_on)):
        if is_on:
            flows_a[i] = math.sqrt(p ** 2 + q ** 2) / (3. ** .5 * v)

    return flows_a


class Grid(object):
    def __init__(self, src_filename, dc_loadflow, new_slack_bus, new_imaps, verbose=False):
        self.filename = src_filename
        self.dc_loadflow = True  # true to compute loadflow with Direct Current model, False for Alternative Cur.
        self.save_io = False  # True to save files (one pretty-print file and one IEEE) for each matpower loadflow comp.
        self.verbose = verbose  # True to print some running logs, including cascading failure depth

        # Container output of Matpower usual functions (mpc structure); contains all grid params/values as dic format
        self.mpc = octave.loadcase(self.filename, verbose=False)
        # Change thermal limits: in IEEE format, they are contaied in 'branch'
        self.thermal_limits = np.asarray(new_imaps)
        self.mpc['branch'][:, 5] = np.asarray(new_imaps)
        self.mpc['branch'][:, 6] = np.asarray(new_imaps)
        self.mpc['branch'][:, 7] = np.asarray(new_imaps)

        self.new_slack_bus = new_slack_bus  # The slack bus is fixed, otherwise loadflow issues
        # Containers that keep in mind the PQ nodes (consumers)
        self.are_loads = np.logical_or(self.mpc['bus'][:, 2] != 0, self.mpc['bus'][:, 3] != 0)

        # Fixed ids of substations associated with prods, loads and lines (init.,  all elements on real substation id)
        self.loads_ids = self.mpc['bus'][self.are_loads, 0]

        self.n_nodes = len(self.mpc['bus'])
        self.n_prods = len(self.mpc['gen'])
        self.n_loads = np.sum(self.are_loads)
        self.n_lines = len(self.mpc['branch'])

        self.ids_lines = np.arange(self.n_lines)

        mapping_permutation, self.number_elements_per_substations = self.compute_topological_mapping_permutation()
        # Topology container: initially, all elements are on the node 0
        self.topology = Topology(prods_nodes=np.zeros((self.n_prods,)), loads_nodes=np.zeros((self.n_loads,)),
                                 lines_or_nodes=np.zeros((self.n_lines,)), lines_ex_nodes=np.zeros((self.n_lines,)),
                                 mapping_array=mapping_permutation)

    def get_number_elements(self):
        return self.n_prods, self.n_loads, self.n_lines

    def get_thermal_limits(self):
        return self.thermal_limits

    def _contains_nan(self):
        voltages = self.mpc['bus'][:, 7:9]
        flows = self.mpc['branch'][:, 13:]
        prods_q = self.mpc['bus'][:, 2]
        has_voltages_nan = np.isnan(voltages).any() or np.any(voltages > 1e10)
        has_flows_nan = np.isnan(flows).any() or np.any(flows > 1e10)
        has_prods_q_nan = np.isnan(prods_q).any() or np.any(prods_q > 1e10)
        return has_voltages_nan or has_flows_nan or has_prods_q_nan

    def extract_flows_a(self, safe_mode=False):
        mpc = self.mpc
        bus = mpc['bus']
        branch = mpc['branch']

        # Compute the per-line Ampere values; column 13 is Pf, 14 Qf
        active = branch[:, 13]  # P
        reactive = branch[:, 14]  # Q
        voltage = np.array([bus[np.where(bus[:, 0] == origin), 7] for origin in branch[:, 0]]).flatten()  # V

        if safe_mode:
            active[active > 1e5] = 1e5
            active[active < -1e5] = -1e5
            reactive[reactive > 1e5] = 1e5
            reactive[reactive < -1e5] = -1e5
            voltage[voltage > 1e2] = 1e2
            voltage[voltage < -1e2] = -1e2
        are_lines_on = self.get_lines_status()
        branches_flows_a = compute_flows_a(active=active, reactive=reactive, voltage=voltage, are_lines_on=are_lines_on)

        return branches_flows_a

    @staticmethod
    def _synchronize_bus_types(mpc, are_loads, new_slack_bus):
        """ This helper is responsible for determining the type of any substation of a grid. This step is mandatory
         prior to compute any loadflow, as matpower is for example expecting a type value of 4 for isolated nodes,
         which ultimately leads to a matpower error if not correctly mentionned in the input grid.

         The function first seeks all substations that are neither origin of extremity of online lines. Their type
         values are put to 4. Then, 2 for the PV nodes values, 3 for the slack bus, and 1 for PQ nodes. """
        bus = mpc['bus']
        gen = mpc['gen']

        # Computes the number of cut loads, and a mask array whether the substation is isolated
        are_isolated_loads, are_isolated_prods, are_isolated_buses = Grid._count_isolated_loads(mpc,
                                                                                                are_loads=are_loads)

        # Retrieve buses with productions (their type needs to be 2)
        bus_prods = gen[:, 0]

        # Check if the slack bus is isolated
        if are_isolated_buses[np.where(bus[:, 0] == new_slack_bus)[0][0]]:
            new_slack_bus = bus_prods[bus_prods != new_slack_bus][0]

        for b, (bus_id, is_isolated) in enumerate(zip(bus[:, 0], are_isolated_buses)):
            # If bus is isolated, put the value 4 for its type (mandatory for matpower)
            if is_isolated:
                bus[b, 1] = 4
            else:  # Otherwise, put 2 for PV node, 1 else
                if bus_id in bus_prods:
                    # If this is the slack bus and a production, then put its type to 3 (slack bus)
                    if int(bus_id) == int(new_slack_bus):
                        bus[b, 1] = 3
                        continue
                    bus[b, 1] = 2
                else:
                    bus[b, 1] = 1

    @staticmethod
    def _count_isolated_loads(mpc, are_loads):
        bus = mpc['bus']
        gen = mpc['gen']
        branch = mpc['branch']

        substations_ids = bus[:, 0]
        prods_ids = gen[:, 0]

        # Retrieves the substations id at the origin or extremity of at least one switched-on line
        branch_online = branch[branch[:, 10] != 0]  # Get switched on lines
        # Unique ids of origin and ext of lines online
        non_isolated_buses, counts = np.unique(branch_online[:, [0, 1]], return_counts=True)
        fully_connected_buses = [bus for bus, count in zip(non_isolated_buses, counts) if count > 0]

        # Compute a mask array whether the substation is isolated
        are_isolated_buses = np.asarray([sub_id not in fully_connected_buses for sub_id in substations_ids])

        # Compute mask whether a substation has a production (PV node)
        are_prods = np.array([g in prods_ids for g in substations_ids])

        return are_isolated_buses[are_loads], are_isolated_buses[are_prods], are_isolated_buses
        #return sum(are_isolated_buses[are_loads]), sum(are_isolated_buses[are_prods]), are_isolated_buses

    def __vanilla_matpower_callback(self, fname_end=None, verbose=False):
        """ Performs a plain matpower callback using octave to compute the loadflow of grid mpc (should be mpc format
        from matpower). This function uses default octave mpoption (they control in certain ways how matpower behaves
        for the loadflow computation.

        :param fname_end: path to the output prettyprint file (produced by matpower) or None to not save this file
        :param verbose: this verbose refers to the matpower verbose: if enabled, will plot the output grid in terminal
        :return: the ouput of matpower (typically mpc structure), and a boolean success of loadflow indicator
        """
        # Fonction of matpower to compute loadflow
        matpower_function = octave.rundcpf if self.dc_loadflow else octave.runpf

        mpopt = octave.mpoption('pf.alg', 'FDBX', 'pf.fd.max_it', 25)

        if self.save_io:
            fname_end = '.m' if fname_end is None else fname_end
            fname = os.path.abspath(os.path.join('tmp', os.path.basename(self.filename)))[:-2] + fname_end
            pprint = os.path.abspath(os.path.join('tmp', 'pp' + os.path.basename(self.filename)))[:-2] + fname_end
            output = matpower_function(self.mpc, mpopt, pprint, fname, verbose=verbose)
        else:
            output = matpower_function(self.mpc, mpopt, verbose=verbose)

        loadflow_success = output['success']  # 0 = failed, 1 = successed
        return output, loadflow_success

    def compute_loadflow(self, fname_end):
        # Ensure that all isolated bus has their type put to 4 (otherwise matpower diverged)
        """ Given the current state of the grid (topology + injections), compute the new loadflow of the grid. This
        function subtreats the Octave pipeline to self.__vanilla_matpower_callback.

        :return: 0 for failed computation, 1 for success
        :raise DivergingLoadflowException: if the loadflow did not converge, raise diverging exception (could be because
        of grid not connexe, or voltages issues, or angle issues etc).
        """
        self._synchronize_bus_types(self.mpc, self.are_loads, self.new_slack_bus)

        try:
            output, loadflow_success = self.__vanilla_matpower_callback(fname_end=fname_end)
        except Oct2PyError:
            raise DivergingLoadflowException(None, 'The grid is not connexe')

        # Save the loadflow output as current grid
        self.mpc = output

        # If matpower returned a diverging computation, raise proper exception
        if not loadflow_success or self._contains_nan():
            raise DivergingLoadflowException(None, 'Power grid outage')

    def load_timestep_injections(self, timestep_injections):
        """ Loads a scenario from class Scenario: contains P and V values for prods, and P and Q values for loads. Other
        timestep entries are loaded using other modules (including pypownet.game).

        :param timestep_injections: an instance of class Scenario
        :return: if do_trigger_lf_computation then the result of self.compute_loadflow else nothing
        """
        assert isinstance(timestep_injections, TimestepEntries), 'Should not happen'

        # Change the filename of self to pretty print middle-end created temporary files
        self.filename = 'scenario%d.m' % (timestep_injections.get_id())

        mpc = self.mpc
        gen = mpc['gen']
        bus = mpc['bus']

        # Import new productions values
        prods_p = timestep_injections.get_prods_p()
        prods_v = timestep_injections.get_prods_v()
        # Check that there are the same number of productions names and values
        assert len(prods_v) == len(prods_p), 'Not the same number of active prods values than reactives prods'
        gen[:, 1] = prods_p
        # Change prods v (divide by bus baseKV); put all to online then negative voltage to offline
        gen[:, 5] = np.asarray(
            prods_v / np.asarray([basekv for i, basekv in zip(bus[:, 0], bus[:, 9]) if i in gen[:, 0]]))
        gen[:, 7] = 1
        gen[prods_v <= 0, 7] = 0

        # Import new loads values
        loads_p = timestep_injections.get_loads_p()
        loads_q = timestep_injections.get_loads_q()
        # Check that there are the same number of productions names and values
        assert len(loads_q) == len(loads_p), 'Not the same number of active loads values than reactives loads'
        bus[self.are_loads, 2] = loads_p
        bus[self.are_loads, 3] = loads_q

    def discard_flows(self):
        self.mpc['branch'] = self.mpc['branch'][:, :13]

    def set_voltage_magnitudes(self, new_voltage_magnitudes):
        self.mpc['bus'][:, 7] = new_voltage_magnitudes

    def set_voltage_angles(self, new_voltage_angles):
        self.mpc['bus'][:, 8] = new_voltage_angles

    def set_lines_status(self, new_lines_status):
        self.mpc['branch'][:, 10] = new_lines_status

    def get_lines_status(self):
        return self.mpc['branch'][:, 10]

    def apply_topology(self, new_topology):
        # Verify new specified topology is of good number of elements and only 0 or 1
        """ Applies a new topology to self. topology should be an instance of class Topology, with computed values to
        be replaced in self.

        :param new_topology: an instance of Topology, with destination values for the nodes values/lines service status
        """
        cpy_new_topology = copy.deepcopy(new_topology)  # Deepcopy as this function sometimes uses to-be-fixed Topology
        assert cpy_new_topology.get_length() == self.get_topology().get_length(), 'Should not happen'
        assert set(cpy_new_topology.get_zipped()).issubset([0, 1]), 'Should not happen'

        # Split topology vector into the four chunks
        new_prods_nodes, new_loads_nodes, new_lines_or_nodes, new_lines_ex_nodes = \
            cpy_new_topology.get_unzipped()

        # Function to find the true id of the substation associated with one node
        node_to_substation = lambda node_id: str(node_id).replace(ARTIFICIAL_NODE_STARTING_STRING, '')

        # Change nodes ids of productions
        gen = self.mpc['gen']
        for p, (prod_id, new_prod_node) in enumerate(zip(self.mpc['gen'][:, 0], new_prods_nodes)):
            prod_substation = node_to_substation(prod_id)
            if new_prod_node == 1:
                gen[p, 0] = float(ARTIFICIAL_NODE_STARTING_STRING + prod_substation)
            else:
                gen[p, 0] = float(node_to_substation(prod_id))

        # Change nodes ids of lines (1 origin and 1 extremity per line)
        branch = self.mpc['branch']
        for li, (line_or_id, new_line_or_node) in enumerate(zip(branch[:, 0], new_lines_or_nodes)):
            line_substation = node_to_substation(line_or_id)
            if new_line_or_node == 1:
                branch[li, 0] = float(ARTIFICIAL_NODE_STARTING_STRING + line_substation)
            else:
                branch[li, 0] = float(line_substation)
        for li, (line_ex_id, new_line_ex_node) in enumerate(zip(branch[:, 1], new_lines_ex_nodes)):
            line_substation = node_to_substation(line_ex_id)
            if new_line_ex_node == 1:
                branch[li, 1] = float(ARTIFICIAL_NODE_STARTING_STRING + line_substation)
            else:
                branch[li, 1] = float(line_substation)

        # Change nodes ids of loads
        bus = self.mpc['bus']
        for lo, (load_node, new_load_node) in enumerate(zip(self.topology.loads_nodes, new_loads_nodes)):
            # If the node on which a load is connected is swap, then swap P and Q values for both nodes
            if new_load_node != load_node:
                are_loads = np.where(self.are_loads[:self.n_nodes // 2], self.are_loads[:self.n_nodes // 2],
                                     self.are_loads[self.n_nodes // 2:])
                id_bus = np.where(are_loads)[0][lo] % (self.n_nodes // 2)
                # Copy first node P and Q
                tmp = copy.deepcopy(bus[id_bus, [2, 3]])
                # Replace their values with the one of its associated node
                bus[id_bus, 2] = bus[(lo + self.n_nodes // 2) % self.n_nodes, 2]
                bus[id_bus, 3] = bus[(lo + self.n_nodes // 2) % self.n_nodes, 3]
                # Paste tmp values into asso. node
                bus[(id_bus + self.n_nodes // 2) % self.n_nodes, 2] = tmp[0]
                bus[(id_bus + self.n_nodes // 2) % self.n_nodes, 3] = tmp[1]
                # Change the ids of current loads
                tmp_id = id_bus if self.are_loads[id_bus] else id_bus + self.n_nodes // 2
                self.are_loads[tmp_id] = False
                self.are_loads[(tmp_id + self.n_nodes // 2) % self.n_nodes] = True

        self.topology = cpy_new_topology

    def get_topology(self):
        return self.topology

    def compute_topological_mapping_permutation(self):
        """ Computes a permutation that shuffles the construction order of a topology (prods->loads->lines or->lines ex)
        into a representation where all elements of a substation are consecutives values (same order, but locally).
        By construction, the topological vector is the concatenation of the subvectors: productions nodes (for each
        value, on which node, 0 or 1, the prod is wired), loads nodes, lines origin nodes, lines extremity nodes and the
        lines service status.

        This function should only be called once, at the instanciation of the grid, for it computes the fixed mapping
        function for the remaining of the game (also fixed along games).
        """
        # Retrieve the true ids of the productions, loads, lines origin (substation id where the origin of a line is
        # wired), lines extremity
        prods_ids = self.mpc['gen'][:, 0]
        loads_ids = self.mpc['bus'][self.are_loads, 0]
        lines_or_ids = self.mpc['branch'][:, 0]
        lines_ex_ids = self.mpc['branch'][:, 1]
        # Based on the default topology construction, compute offset of subvectors
        loads_offset = self.n_prods
        lines_or_offset = self.n_prods + self.n_loads
        lines_ex_offset = self.n_prods + self.n_loads + self.n_lines

        # Get the substations ids (discard the artificially created ones, i.e. half end)
        substations_ids = self.mpc['bus'][:self.n_nodes // 2, 0]

        # First, loop throug all the substations, and count the number of elements per substation
        substations_n_elements = []
        for node_id in substations_ids:
            n_prods = (prods_ids == node_id).sum()
            n_loads = (loads_ids == node_id).sum()
            n_lines_or = (lines_or_ids == node_id).sum()
            n_lines_ex = (lines_ex_ids == node_id).sum()
            n_elements = n_prods + n_loads + n_lines_or + n_lines_ex
            substations_n_elements.append(n_elements)
        assert sum(substations_n_elements) == len(prods_ids) + len(loads_ids) + len(lines_or_ids) + len(lines_ex_ids)
        # Based on the number of elements per substations, store the true id of substations with less than 4 elements
        mononode_substations = substations_ids[np.where(np.array(substations_n_elements) < 4)[0]]

        mapping = []
        # Loop through all of the substations (first half of all buses of reference grid), then loop successively if
        # its id is also: a prod, a load, a line origin, a line extremity. For each of these cases, node_mapping stores
        # the index of the id respectively to the other same objects (e.g. store 0 for prod of substation 1, because
        # it is the first prod of the prods id list self.mpc['gen'][:, 0]
        for node_id in substations_ids:  # Discard artificially created buses
            node_mapping = []
            if node_id in prods_ids:
                node_index = np.where(prods_ids == node_id)[0][0]  # Only one prod per substation
                node_mapping.append(node_index)  # Append because at most one production per substation
            if node_id in loads_ids:
                node_index = np.where(loads_ids == node_id)[0][0] + loads_offset  # Only one load per subst.
                node_mapping.append(node_index)  # Append because at most one consumption per substation
            if node_id in lines_or_ids:
                node_index = np.where(lines_or_ids == node_id)[0] + lines_or_offset  # Possible multiple lines per subst
                node_mapping.extend(node_index)  # Extend because a substation can have multiple lines as their origin
            if node_id in lines_ex_ids:
                node_index = np.where(lines_ex_ids == node_id)[0] + lines_ex_offset
                node_mapping.extend(node_index)  # Extend because a substation can have multiple lines as their extrem.
            mapping.append(node_mapping)
        assert len(mapping) == self.n_nodes // 2, 'Mapping does not have one configuration per substation'

        # Verify that the mapping array has unique values and of expected size (i.e. same as concatenated-style one)
        assert len(np.concatenate(mapping)) == len(
            np.unique(np.concatenate(mapping))), 'Mapping does not have unique values, should not happen'
        assert sum([len(m) for m in mapping]) == self.n_prods + self.n_loads + 2 * self.n_lines, \
            'Mapping does not have the same number of elements as there are in the grid'

        return mapping, substations_n_elements

    def export_to_observation(self):
        """ Exports the current grid state into an observation. """
        mpc = self.mpc
        bus = mpc['bus']
        gen = mpc['gen']
        branch = mpc['branch']

        # Lists and arrays helpers
        to_array = lambda array: np.asarray(array)
        nodes_to_substations = lambda array: list(
            map(lambda x: int(float(x)),
                list(map(lambda v: str(v).replace(ARTIFICIAL_NODE_STARTING_STRING, ''), array))))

        # Generators data
        active_prods = to_array(gen[:, 1])  # Pg
        reactive_prods = to_array(gen[:, 2])  # Qg
        voltage_prods = to_array(gen[:, 5])  # Vg
        substations_ids_prods = to_array(nodes_to_substations(gen[:, 0]))

        # Branch data origin
        active_flows_origin = to_array(branch[:, 13])  # Pf
        reactive_flows_origin = to_array(branch[:, 14])  # Qf
        voltage_origin = to_array([bus[np.where(bus[:, 0] == origin), 7] for origin in branch[:, 0]]).flatten()
        substations_ids_lines_or = to_array(nodes_to_substations(branch[:, 0]))
        # Branch data extremity
        active_flows_extremity = to_array(branch[:, 15])  # Pt
        reactive_flows_extremity = to_array(branch[:, 16])  # Qt
        voltage_extremity = to_array([bus[np.where(bus[:, 0] == origin), 7] for origin in branch[:, 1]]).flatten()
        substations_ids_lines_ex = to_array(nodes_to_substations(branch[:, 1]))

        thermal_limits = branch[:, 5]
        ampere_flows = self.extract_flows_a()

        # Loads data
        loads_buses = bus[self.are_loads, :]  # Select lines of loads buses
        active_loads = to_array(loads_buses[:, 2])
        reactive_loads = to_array(loads_buses[:, 3])
        voltage_loads = to_array(loads_buses[:, 7])
        substations_ids_loads = to_array(nodes_to_substations(bus[:, 0][self.are_loads]))

        # Retrieve isolated buses
        are_isolated_loads, are_isolated_prods, are_isolated_buses = self._count_isolated_loads(mpc,
                                                                                                are_loads=self.are_loads)

        # Topology vector
        topology = self.get_topology().get_zipped()  # Retrieve concatenated version of topology
        lines_status = branch[:, 10].astype(int)

        return pypownet.environment.Observation(active_loads, reactive_loads, voltage_loads, active_prods,
                                                reactive_prods, voltage_prods, active_flows_origin,
                                                reactive_flows_origin, voltage_origin, active_flows_extremity,
                                                reactive_flows_extremity, voltage_extremity, ampere_flows,
                                                thermal_limits, topology, lines_status,
                                                are_isolated_loads, are_isolated_prods,
                                                substations_ids_loads, substations_ids_prods,
                                                substations_ids_lines_or, substations_ids_lines_ex,
                                                timesteps_before_lines_reconnectable=None,
                                                timesteps_before_planned_maintenance=None)  # kwargs set by game

    def export_lines_capacity_usage(self, safe_mode=False):
        """ Computes and returns the lines capacity usage, i.e. the elementwise division of the flows in Ampere by the
            lines nominal thermal limit.

            :return: a list of size the number of lines of positive values
            """
        mpc = self.mpc
        branch = mpc['branch']
        to_array = lambda array: np.asarray(array)

        # Compute flows in Ampere using formula compute_flows_a
        flows_a = self.extract_flows_a(safe_mode)
        lines_capacity_usage = to_array(flows_a / branch[:, 5])  # elementwise division of flow a and rateA

        return lines_capacity_usage

    def _snapshot(self, dst_fname=None):
        """ Saves a snapshot of current grid state into IEEE format file with path dst_fname. """
        self._synchronize_bus_types(self.mpc, self.are_loads, self.new_slack_bus)
        if dst_fname is None:
            dst_fname = os.path.abspath(os.path.join('tmp', 'snapshot_' + os.path.basename(self.filename)))
            if not os.path.exists('tmp'):
                os.makedirs('tmp')
        print('Saved snapshot at', dst_fname)
        return octave.savecase(dst_fname, self.mpc)


class Topology(object):
    """
    This class is a container for the topology lists defining the current topological state of a grid. Topology should
    be manipulated using this class, as it maintains the adopted convention consistently.
    """

    def __init__(self, prods_nodes, loads_nodes, lines_or_nodes, lines_ex_nodes, mapping_array):
        self.prods_nodes = prods_nodes
        self.loads_nodes = loads_nodes
        self.lines_or_nodes = lines_or_nodes
        self.lines_ex_nodes = lines_ex_nodes

        # Function that sorts the internal topological array into a more intuitive representation: the nodes of the
        # elements of a substation are consecutive (first prods, then loads, then lines origin, then line ext.)
        concatenated_mapping_permutation = np.concatenate(mapping_array)
        self.mapping_permutation = lambda array: [int(array[c]) for c in concatenated_mapping_permutation]
        invert_indexes = [np.where(concatenated_mapping_permutation == i)[0][0] for i in
                          range(len(concatenated_mapping_permutation))]
        self.invert_mapping_permutation = lambda array: [int(array[c]) for c in invert_indexes]

        self.mapping_array = mapping_array

    def get_zipped(self):
        return self.mapping_permutation(np.concatenate(
            (self.prods_nodes, self.loads_nodes, self.lines_or_nodes, self.lines_ex_nodes)))

    def get_unzipped(self):
        return self.prods_nodes, self.loads_nodes, self.lines_or_nodes, self.lines_ex_nodes

    @staticmethod
    def unzip(topology, n_prods, n_loads, n_lines, invert_mapping_function):
        # Shuffle topology parameter based on index positions; invert_mapping_function should be the same as the
        # one used by the environment to convert the sorted topology into its internal representation
        topology_shuffled = invert_mapping_function(topology)
        assert len(topology_shuffled) == n_prods + n_loads + 2 * n_lines

        prods_nodes = topology_shuffled[:n_prods]
        loads_nodes = topology_shuffled[n_prods:n_prods + n_loads]
        lines_or_nodes = topology_shuffled[-2 * n_lines:-n_lines]
        lines_ex_nodes = topology_shuffled[-n_lines:]
        return prods_nodes, loads_nodes, lines_or_nodes, lines_ex_nodes

    def get_length(self):
        return len(self.prods_nodes) + len(self.loads_nodes) + len(self.lines_ex_nodes) + len(self.lines_or_nodes)

    def __deepcopy__(self, memo):
        cpy = object.__new__(type(self))
        cpy.prods_nodes = copy.deepcopy(self.prods_nodes)
        cpy.loads_nodes = copy.deepcopy(self.loads_nodes)
        cpy.lines_or_nodes = copy.deepcopy(self.lines_or_nodes)
        cpy.lines_ex_nodes = copy.deepcopy(self.lines_ex_nodes)
        cpy.mapping_array = self.mapping_array
        cpy.mapping_permutation = self.mapping_permutation
        cpy.invert_mapping_permutation = self.invert_mapping_permutation
        return cpy

    def __str__(self):
        return 'Grid topology: %s' % ('[%s]' % ', '.join(list(map(str, self.get_zipped()))))

