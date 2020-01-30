import yaml
import numpy as np
import pickle
import os
import sys
import simtk.unit as unit
import logging
from perses.utils.data import load_smi

from perses.samplers.multistate import HybridSAMSSampler, HybridRepexSampler
from perses.annihilation.relative import HybridTopologyFactory
from perses.app.relative_setup import NonequilibriumSwitchingFEP, RelativeFEPSetup
from perses.annihilation.lambda_protocol import LambdaProtocol
from perses.rjmc.topology_proposal import TopologyProposal, SmallMoleculeSetProposalEngine
from perses.rjmc.geometry import FFAllAngleGeometryEngine
from perses.app.simulation import Simulation

from openmmtools import mcmc
from openmmtools.multistate import MultiStateReporter, sams, replicaexchange
from perses.utils.smallmolecules import render_atom_mapping
from perses.tests.utils import validate_endstate_energies
from openmoltools import forcefield_generators
from perses.utils.openeye import *
from perses.app.utils import *
from perses.dispersed.utils import generalized_worker_class_instantiation, call_worker_class_method
import mdtraj as md
import simtk.openmm.app as app
import simtk.openmm as openmm
from io import StringIO
import copy

#import perses dask Client
from perses.app.relative_setup import DaskClient

logging.basicConfig(level = logging.NOTSET)
_logger = logging.getLogger("NetworkBuilder")
_logger.setLevel(logging.DEBUG)

ENERGY_THRESHOLD = 1e-4
from openmmtools.constants import kB

class NetworkBuilder(object):
    """
    Create a NetworkX graph representing the set of chemical states to be sampled.
    Vertices represent nonalchemical states (i.e. ligands/protein mutants).
    Edges represent the alchemical transformations between nonalchemical states.
    Specifically, this class creates topology proposals, geometry_proposals, HybridTopologyFactory objects, and Simulation objects for each edge.
    """
    proposal_arguments = {'pressure': 1.0 * unit.atmosphere,
                         'temperature': 300.0 * unit.kelvin,
                         'solvent_padding': 9.0 * unit.angstroms,
                         'hmass': 4 * unit.amus,
                         'map_strength': 'default',
                         'phases': ['vacuum', 'solvent', 'complex'],
                         'forcefield_files': ['amber/protein.ff14SB.xml', 'amber/tip3p_standard.xml', 'amber/tip3p_HFE_multivalent.xml'],
                         'small_molecule_forcefield' : 'openff-1.0.0',
                         'small_molecule_parameters_cache' : 'cache.json',
                         'neglect_angles': False,
                         'anneal_14s': False,
                         'water_model': 'tip3p',
                         'use_dispersion_correction': False,
                         'softcore_alpha': None,
                         'bond_softening_constant': 1.0,
                         'angle_softening_constant': 1.0,
                         'soften_only_new': False,
                         'softcore_LJ_v2': True,
                         'softcore_electrostatics': True,
                         'softcore_LJ_v2_alpha': 0.85,
                         'softcore_electrostatics_alpha': 0.3,
                         'softcore_sigma_Q': 1.0}

    simulation_arguments = {
                             'repex':{
                                      ##Hybrid Sampler##
                                      'timestep': 4 * unit.femtoseconds,
                                      'collision_rate': 5. / unit.picoseconds,
                                      'n_steps_per_move_application': 1,
                                      'reassign_velocities': False,
                                      'n_restart_attempts': 20,
                                      'splitting': "V R O R V",
                                      'constraint_tolerance' : 1e-6,
                                      'offline_freq': 10,

                                      ##Setup##
                                      'n_states': 13,
                                      'temperature': 300.0 * unit.kelvin,
                                      'atom_selection': "not water",
                                      'checkpoint_interval': 100,
                                      'lambda_protocol': 'default',
                                      'trajectory_directory': 'repex_{index0}_to_{index1}',
                                      'trajectory_prefix': '{phase}',

                                      ##Equilibrate##
                                      "n_equilibration_iterations": 1,

                                      ##Extend##
                                      'n_cycles': 1000
                                      },

                             'sams': {'flatness_criteria': 'minimum-visits',
                                      'gamma0': 1.,
                                      #the rest of the arguments are held by 'repex', which will be updated momentarily
                                     },

                             'smc': {
                                     ##__init__##
                                     'lambda_protocol': 'default',
                                     'temperature': 300.0 * unit.kelvin,
                                     'trajectory_directory': 'neq_{index0}_to_{index1}',
                                     'trajectory_prefix': '{phase}',
                                     'atom_selection': "not water",
                                     'timestep': 4 * unit.femtoseconds,
                                     'collision_rate': 1. / unit.picoseconds,
                                     'eq_splitting_string': 'V R O R V',
                                     'neq_splitting_string': 'V R O R V',
                                     'ncmc_save_interval': None,
                                     'measure_shadow_work': False,
                                     'neq_integrator': 'langevin',
                                     'compute_endstate_correction': True,
                                     'external_parallelism': None,
                                     'internal_parallelism': {'library': ('dask', 'LSF'),
                                                              'num_processes': 1},

                                     ##sMC_anneal##
                                     'num_particles': 100,
                                     'protocols': {'forward': np.linspace(0,1, 1000),
                                                   'reverse': np.linspace(1,0,1000)},
                                     'directions': ['forward', 'reverse'],
                                     'num_integration_steps' : 1,
                                     'return_timer': False,
                                     'rethermalize': False,
                                     'trailblaze': None,
                                     'resample': None,

                                     ##equilibrate##
                                     'n_equilibration_iterations': 1,
                                     'n_steps_per_equilibration': 5000,
                                     'endstates': [0,1],
                                     'max_size': 1024*1e3,
                                     'decorrelate': True,
                                     'timer': False,
                                     'minimize': False
                                     }
                             }
    #update common simulation arguments
    simulation_arguments['sams'].update(simulation_arguments['repex'])
    simulation_arguments['sams'].update({'trajectory_directory': 'sams_{index0}_to_{index1}'})


    known_phases = ['vacuum', 'solvent', 'complex'] # we omit complex phase in the known_phases if a receptor_filename is None
    assert set(proposal_arguments['phases']).issubset(set(known_phases)), f"the default proposal_arguments['phases'] is not a subset of the known_phases"
    supported_connectivities = {'fully_connected': generate_fully_connected_adjacency_matrix} #we can add other options later, but this is a good vanilla one to start with

    def __init__(self,
                 parallelism,
                 ligand_input,
                 ligand_indices = None,
                 receptor_filename = None,
                 graph_connectivity = 'fully_connected',
                 proposal_parameters = None,
                 simulation_parameters = ('repex', None)):
        """
        Initialize NetworkX graph and build connectivity with a `graph_connectivity` input.

        Arguments
        ----------
        parallelism : perses.dispersed.Parallelism object (activated)
            parallelism to perform edge calculations

        ligand_input : str
            the name of the ligand file (any openeye supported format)
            this can either be an .sdf or list of .sdf files, or a list of SMILES strings

        ligand_indices : list of int
            indices in the ligand input to parse

        receptor_filename : str, default None
            Receptor mol2 or pdb filename. If None, complex leg will be omitted.

        graph_connectivity : str or np.matrix or list of tuples, default 'fully_connected'
            The graph connectivity information for the experiment.  This accepts one of several allowable
            strings (corresponding to connectivity defaults), a 2d np.array specifying the explicit connectivity
            matrix for indexed ligands, or a list of tuples corresponding to pairwise ligand index connections.
            The default is 'fully_connected', which specified a fully connected (i.e. complete or single-clique) graph.
            If the graph_connectivity is input as a 2d numpy array, values represent the weights of the transform.  0 weights entail no connectivity.
            The self.adjacency_matrix argument produced as a result contains the log(weights) of the transform as entries.

        proposal_parameters: dict, default None
            The following dict is parseable from a setup yaml, but have defaults given if no setup yaml is given.
            They mostly consist of the thermodynamic state of the graph, several potential modifications to the
            hybrid factory (i.e. the alchemical system), the single-topology mapping criteria, and the sampler-specific
            parameters (i.e. parameters specific to Replica Exchange, SAMS, and sMC.)

            forcefield_files : list of str
                The list of ffxml files that contain the forcefields that will be used
            pressure : Quantity, units of pressure
                Pressure to use in the barostat
            temperature : Quantity, units of temperature
                Temperature to use for the Langevin integrator
            solvent_padding : Quantity, units of length
                The amount of padding to use when adding solvent
            neglect_angles : bool
                Whether to neglect certain angle terms for the purpose of minimizing work variance in the RJMC protocol.
            anneal_14s : bool, default False
                Whether to anneal 1,4 interactions over the protocol;
                    if True, then geometry_engine takes the argument use_14_nonbondeds = False;
                    if False, then geometry_engine takes the argument use_14_nonbondeds = True;

        simulation_parameters : tuple(str, (dict or None)) or dict, default ('repex', None)
            the simulation parameters to put into the appropriate simulation object
            if type(simulation_parameters) == tuple:
                #then the 0th entry is a string given by 'repex', 'sams', or 'smc', the flavor of simulation
                #and the 1st entry is a dict of parameters that are appropriate to the flavor of simulation
                #if dict is None, then default 'repex' parameters will be used
            elif type(simulation_parameters) == dict, each entry is a {(int, int): {<phase>: (<simulation_flavor>, dict(params))}};
                if an transformation entry is missing, it is defaulted as above

        TODO:
        1. change the name of 'proposal_arguments' to something more appropriate.
        2. allow custom atom mapping for all edges in graph.  currently, we can only specify one of three mapping schemes for all molecules
        3. if 'receptor_filename' is None, remove complex phase from the proposal arguments; otherwise, the 'phases' will have to be specified explicitly
        4. parallelize setup by defining `resources`
        5. update `manipulate_edge_post_hoc`
        """
        self.parallelism = parallelism
        assert hasattr(self.parallelism, 'client'), f"the parallelism object must be activated (i.e. have a client)"

        self.ligand_input = ligand_input
        self.ligand_indices = ligand_indices
        self.receptor_filename = receptor_filename
        self._parse_ligand_input()

        #Now we must create some defaults for thermodynamic states
        _logger.debug(f"kwargs: {proposal_parameters}")
        self._create_proposal_parameters(proposal_parameters)
        self.adjacency_matrix = self._create_connectivity_matrix(graph_connectivity)
        self._validate_simulation_parameters(simulation_parameters)
        self.beta = 1.0 / (kB * self.proposal_arguments['temperature'])

        #Now we can create a system generator for each phase.
        self._create_system_generator()

        #Now create the proposal engine
        self.proposal_engine = SmallMoleculeSetProposalEngine(self.smiles_list,
                                                              self.system_generator,
                                                              map_strength = self.proposal_arguments['map_strength'],
                                                              residue_name='MOL')
        #create a geometry engine
        self.geometry_engine = FFAllAngleGeometryEngine(metadata=None,
                                                        use_sterics=False,
                                                        n_bond_divisions=100,
                                                        n_angle_divisions=180,
                                                        n_torsion_divisions=360,
                                                        verbose=True,
                                                        storage=None,
                                                        bond_softening_constant=1.0,
                                                        angle_softening_constant=1.0,
                                                        neglect_angles = self.proposal_arguments['neglect_angles'],
                                                        use_14_nonbondeds = not self.proposal_arguments['anneal_14s'])

        #create the Network
        self._create_network()

        _logger.info(f"Network construction complete.")


    def _create_network(self):
        """
        This is the main function of the class.  It builds a networkx graph on all of the transformations.
        """
        import networkx as nx
        self.network = nx.DiGraph()

        #first, let's add the nodes
        for index, smiles in enumerate(self.smiles_list):
            node_attribs = {'smiles': smiles}
            self.network.add_node(index, **node_attribs)

        #then, let's add the edge creation arguments
        _kwargs = []
        for index, log_weight in np.ndenumerate(self.adjacency_matrix):
            i, j = index[0], index[1]
            if log_weight != -np.inf:
                _kwarg_dict = {'start_index': i, 'end_index': j, 'weight': np.exp(log_weight), 'edge_simulation_parameters': None}
                _kwargs.append(_kwarg_dict)
        num_runs = len(_kwargs)

        #create NetworkBuilder objects on the workers
        remote_worker = True if self.parallelism.client is not None else self
        _class = None if remote_worker == True else self
        creation_validations = self.parallelism.run_all(func = generalized_worker_class_instantiation,
                                                        arguments = (remote_worker, 'build_network_object', _class),
                                                        workers = list(self.parallelism.workers.values()))

        #creation_validations might be a bool if we are running locally
        if self.parallelism.client is None:
            assert creation_validations, f"scattered_build_network_instantiation_futures failed"
        else:
            assert all(entry == True for entry in creation_validations), f"scattered_build_network_instantiation_futures failed"

        #now we have to deploy the jobs
        run_futures = self.parallelism.deploy(func = call_worker_class_method,
                                              arguments = tuple([[remote_worker] * num_runs,
                                                                ['build_network_object'] * num_runs,
                                                                ['_create_network_edge'] * num_runs,
                                                                _kwargs]),
                                              workers = list(self.parallelism.workers.values()))
        self.parallelism.progress(run_futures)
        run_results = self.parallelism.gather_results(futures = run_futures,
                                                      omit_errors = False)

        #now we simply iterate through the entire run_results and add to the local network
        for _kwarg_input_dict, return_dict in zip(_kwargs, run_results):
            i, j = _kwarg_input_dict['start_index'], _kwarg_input_dict['end_index']
            adjacency_matrix_log_weight = return_dict['adjacency_matrix_log_weight']
            proposals = return_dict['proposals']
            hybrid_factory_dict = return_dict['hybrid_factory_dict']
            endstate_energy_errors_dict = return_dict['endstate_energy_errors_dict']
            simulation_dict = return_dict['simulation_dict']
            validation_dict = return_dict['validation_dict']

            return_phases = list(proposals.keys())

            #now add to the digraph
            self.network.add_edge(i, j)
            self.network.edges[i, j]['proposals'] = proposals
            for _phase in return_phases:
                self.network.edges[i, j]['proposals'][_phase]['hybrid_factory'] = hybrid_factory_dict[_phase]
                self.network.edges[i, j]['proposals'][_phase]['endstate_energy_errors'] = endstate_energy_errors_dict[_phase]
                self.network.edges[i, j]['proposals'][_phase]['simulation'] = simulation_dict[_phase]
                self.network.edges[i, j]['proposals'][_phase]['validated'] = validation_dict[_phase]

            #check if we can validate all of the phases in the edge
            #this is True if the adjacency_matrix_log_weight is not -np.inf
            if return_dict['adjacency_matrix_log_weight'] != -np.inf:
                #then we can give the adjacency matrix the go-ahead for this edge
                self.network.edges[i,j]['validated'] = True
                self.adjacency_matrix[i,j] = return_dict['adjacency_matrix_log_weight']
            else:
                self.network.edges[i,j]['validated'] = False
                self.adjacency_matrix[i,j] = -np.inf


        #make the adjacency_matrix a graph attribute
        self.network.graph['adjacency_matrix'] = self.adjacency_matrix


    def manipulate_edge_post_hoc(self,
                                 start_index,
                                 end_index,
                                 weight = 1.,
                                 edge_simulation_parameters = None):
        """
        This is a function to add network edges post hoc if the practitioner decides another edge is necessary

        Arguments
        ---------
        start_index : int
            int index of the starting ligand (see self.ligand_list)
        end_index : int
            int index of the ligand that the edge points to (from start_index)
        weight : float, default 1.0
            weight of the edge
        edge_simulation_parameters : dict or None
            the simulation parameters to put into the appropriate simulation object;
            if type(edge_simulation_parameters) == dict, then the object is a {<phase>: (<simulation_flavor>, dict(params))};
                if an transformation entry is missing, it is defaulted as above
            elif edge_simulation_parameters is None:
                default parameters or self.simulation_parameters will be used to create the edge simulation object

        Returns
        -------
        success : bool
            whether the intended edge was added and validated
        """
        success = self._create_network_edge(start_index = start_index,
                                        end_index = end_index,
                                        weight = np.exp(log_weight),
                                        edge_simulation_parameters = edge_simulation_parameters)

        self.network.graph['adjacency_matrix'][start_index, end_index] = self.adjacency_matrix[start_index, end_index]

        return success

    def _create_network_edge(self,
                            start_index,
                            end_index,
                            weight,
                            edge_simulation_parameters):
        """
        This is a method to create the necessary parameters for a network edge;
        this method can be called on a remote worker directly

        Arguments
        ---------
        start_index : int
            int index of the starting ligand (see self.ligand_list)
        end_index : int
            int index of the ligand that the edge points to (from start_index)
        weight : float, default 1.0
            weight of the edge
        edge_simulation_parameters : dict or None
            the simulation parameters to put into the appropriate simulation object;
            if type(edge_simulation_parameters) == dict, then the object is a {<phase>: (<simulation_flavor>, dict(params))};
                if an transformation entry is missing, it is defaulted as above
            elif edge_simulation_parameters is None:
                default parameters or self.simulation_parameters will be used to create the edge simulation object

        Returns
        -------
        returnables : dict
            adjacency_matrix_log_weight : float
                the log weight of the adjacency matrix entry i,j
            proposals : dict
                dictionary of phase geometry and topology proposals
            hybrid_factory_dict : dict
                dictionary of {_phase : HybridTopologyFactory}
            endstate_energy_errors_dict : dict
                dictionary of {_phase : endstate_energy_errors <tuple>}
            simulation_dict : dict
                dictionary of {_phase : Simulation <object>}
            validation_dict : dict
                dictionary of {_phase : validated <bool>}
        """
        i, j = start_index, end_index
        log_weight = np.log(weight)
        _logger.info(f"creating proposals for edge {(i,j)}")

        #initialize returnables
        returnables = {'adjacency_matrix_log_weight' : None,
                       'proposals' : {},
                       'hybrid_factory_dict': {},
                       'endstate_energy_errors_dict' : {},
                       'simulation_dict' : {},
                       'validation_dict' : {}
                      }


        if i == j: #we refuse to make a transformation from a node to itself
            if not np.isinf(log_weight):
                _logger.warning(f"\tthe log weight of the self-transition for ligand {i} is not -np.inf; treating as such...")
                returnables.update({'adjacency_matrix_log_weight' : -np.inf})
            return returnables

        if not np.isinf(log_weight):
            current_oemol, current_positions, current_topology = self.ligand_oemol_pos_top[i]
            proposed_oemol, proposed_positions, proposed_topology = self.ligand_oemol_pos_top[j]
            _logger.info(f"\tcreating topology and geometry proposals.")
            proposals =  self._generate_proposals(current_oemol = current_oemol,
                                                  proposed_oemol = proposed_oemol,
                                                  current_positions = current_positions,
                                                  current_topology = current_topology)
            returnables.update({'proposals': proposals})
            #self.network.edges[i,j]['proposals'] = proposals

            _logger.info(f"\tcreating hybrid factories.  iterating through proposal phases...")
            for _phase, property_dict in proposals.items():
                _logger.info(f"\t\tcreating hybrid_factory for phase {_phase}")

                try:
                    hybrid_factory = HybridTopologyFactory(topology_proposal = property_dict['topology_proposal'],
                                                           current_positions = property_dict['current_positions'],
                                                           new_positions = property_dict['proposed_positions'],
                                                           use_dispersion_correction = self.proposal_arguments['use_dispersion_correction'],
                                                           functions=None,
                                                           softcore_alpha = self.proposal_arguments['softcore_alpha'],
                                                           bond_softening_constant = self.proposal_arguments['bond_softening_constant'],
                                                           angle_softening_constant = self.proposal_arguments['angle_softening_constant'],
                                                           soften_only_new = self.proposal_arguments['soften_only_new'],
                                                           neglected_new_angle_terms = property_dict['forward_neglected_angles'],
                                                           neglected_old_angle_terms = property_dict['reverse_neglected_angles'],
                                                           softcore_LJ_v2 = self.proposal_arguments['softcore_LJ_v2'],
                                                           softcore_electrostatics = self.proposal_arguments['softcore_electrostatics'],
                                                           softcore_LJ_v2_alpha = self.proposal_arguments['softcore_LJ_v2_alpha'],
                                                           softcore_electrostatics_alpha = self.proposal_arguments['softcore_electrostatics_alpha'],
                                                           softcore_sigma_Q = self.proposal_arguments['softcore_sigma_Q'],
                                                           interpolate_old_and_new_14s = self.proposal_arguments['anneal_14s'])

                    endstate_energy_errors = validate_endstate_energies(topology_proposal = property_dict['topology_proposal'],
                                                                        htf = hybrid_factory,
                                                                        added_energy = property_dict['added_valence_energy'],
                                                                        subtracted_energy = property_dict['subtracted_valence_energy'],
                                                                        beta = self.beta,
                                                                        ENERGY_THRESHOLD = ENERGY_THRESHOLD)

                    _logger.info(f"\t\tendstate energies validated to within {ENERGY_THRESHOLD}!")

                    #now we have to add the simulation object on the edge's appropriate phase
                    if edge_simulation_parameters is None:
                        phase_edge_simulation_parameters = None
                        #then we have to try to pull self.simulation_parameters
                        if self.simulation_parameters is not None:
                            if (start_index, end_index) in list(self.simulation_parameters.keys()):
                                if _phase in list(self.simulation_parameters[(start_index, end_index)]):
                                    phase_edge_simulation_parameters = {_phase: self.simulation_parameters[(start_index, end_index)][_phase]}
                    else:
                        phase_edge_simulation_parameters = {_phase: edge_simulation_parameters[_phase]}


                    simulation_object = self._create_simulation_object(hybrid_factory = hybrid_factory,
                                                                       forward_index_map = (i,j),
                                                                       phase = _phase,
                                                                       edge_simulation_parameters = phase_edge_simulation_parameters)

                    #now to append to returnables
                    returnables['hybrid_factory_dict'].update({_phase : hybrid_factory})
                    returnables['endstate_energy_errors_dict'].update({_phase : endstate_energy_errors})
                    returnables['simulation_dict'].update({_phase : simulation_object})
                    returnables['validation_dict'].update({_phase : True})

                except Exception as e:
                    _logger.warning(f"\t\t{e}")
                    _logger.warning(f"\t\tdetected failure to validate system.  omitting this edge.")

                    #now to append to returnables
                    returnables['hybrid_factory_dict'].update({_phase : None})
                    returnables['endstate_energy_errors_dict'].update({_phase : None})
                    returnables['simulation_dict'].update({_phase : None})
                    returnables['validation_dict'].update({_phase : False})

                if all(returnables['validation_dict'].values()):
                    returnables.update({'adjacency_matrix_log_weight' : log_weight})
                else:
                    returnables.update({'adjacency_matrix_log_weight' : -np.inf})

            return returnables

    def _create_simulation_object(self,
                                  hybrid_factory,
                                  forward_index_map,
                                  phase,
                                  edge_simulation_parameters = None):
        """

        Arguments
        ---------
        hybrid_factory : perses.annihilation.relative.HybridTopologyFactory
            hybrid factory with which a simulation object will be created
        forward_index_map : tuple(index0, index1)
            indices of edge transform
        phase : str
            the phase of the transformation
        edge_simulation_parameters : dict or None
            the simulation parameters to put into the appropriate simulation object;
            NOTE: in this case, the edge_simulation_parameters may only have one key specific to the phase being generated;
            this is so that we may use the `simulation_parameter_assertions` method to avoid code duplication.
            if type(edge_simulation_parameters) == dict, each entry is a {<phase>: (<simulation_flavor>, dict(params))};
                if an transformation entry is missing, it is defaulted as above
            elif edge_simulation_parameters is None:
                default parameters or self.simulation_parameters will be used to create the edge simulation object

        Returns
        -------
        simulation_object : perses.app.simulation.Simulation
            Simulation object appropriate to the input
        """
        #assert/create the simulation parameter arguments
        if edge_simulation_parameters is not None:
            assert len(list(edge_simulation_parameters.keys())) == 1, f"the edge_simulation_parameters can only have one phase-specific key"
            self.simulation_parameter_assertions(edge_simulation_parameters)
            _phase = list(edge_simulation_parameters.keys())[0]
            _simulation_flavor = edge_simulation_parameters[_phase][0]
            _simulation_parameters = copy.deepcopy(edge_simulation_parameters[_phase][1])
        else:
            #check if we have a global_simulation_flavor
            if type(self.global_simulation_flavor) == str:
                assert self.simulation_parameters == None, f"if the global_simulation_flavor is set, then the self.simulation_parameters must be None"
                #then we have a default
                _simulation_flavor = self.global_simulation_flavor
                _simulation_parameters = copy.deepcopy(self.simulation_arguments[_simulation_flavor])
            else:
                raise Exception(f"if the global simulation flavor is not set, then there are no appropriate defaults")

        #modify the trajectory directory and trajectory_prefix
        _simulation_parameters.update({'trajectory_directory': _simulation_parameters['trajectory_directory'].format(index0 = str(forward_index_map[0]), index1 = str(forward_index_map[1]))})
        _simulation_parameters.update({'trajectory_prefix': _simulation_parameters['trajectory_prefix'].format(phase = phase)})

        #now create a simulation object
        simulation_object = Simulation(hybrid_factory = hybrid_factory,
                                       sampler_type = _simulation_flavor,
                                       sampler_arguments = _simulation_parameters)
        return simulation_object


    def _create_connectivity_matrix(self, graph_connectivity):
        """
        create a numpy 2d adjacency matrix for the digraph.

        Arguments
        ---------
        graph_connectivity : str or np.array() (2D)
            the key or explicit connectivity matrix with weights

        Returns
        -------
        adjacency_matrix : np.array (2d)
            adjacency matrix used to specify weights
        """
        if type(graph_connectivity) == str:
            assert graph_connectivity in self.supported_connectivities, f"{graph_connectivity} is not supported.  Supported arguments are {self.supported_connectivities}."
            return self.supported_connectivities[graph_connectivity](len(self.smiles_list))
        elif type(graph_connectivity) == np.ndarray:
            #we simply convert the weights into log weights
            return np.log(graph_connectivity)


    def _parse_ligand_input(self):
        """
        Parse the ligand input.
        Creates the following attributes:
        1. self.ligand_oemol_pos_top : list of tuple(oemol, position, topology)
        2. self.ligand_ffxml : xml for ligands
        3. self.smiles_list : list of smiles
        4. self.ligand_md_topologies : list of mdtraj.Topology objects
                                       corresponding to self.ligand_oemol_pos_top topologies.
        """
        self.ligand_oemol_pos_top = []
        if type(self.ligand_input) == str: # the ligand has been provided as a single file
            _logger.debug(f"ligand input is a str; checking for .smi and .sdf file.")
            if self.ligand_input[-3:] == 'smi':
                _logger.info(f"Detected .smi format.  Proceeding...")
                self.smiles_list = load_smi(self.ligand_input, index = self.ligand_indices)

                #create a ligand data list to hold all ligand oemols, systems, positions, topologies
                from openforcefield.topology import Molecule
                self.molecules = list() # openforcefield Molecule objects
                for smiles in self.smiles_list:
                    _logger.debug(f"creating oemol, system, positions, and openmm.Topology for smiles: {smiles}...")
                    # TODO: Should we avoid createSystemFromSMILES?
                    oemol, system, positions, topology = createSystemFromSMILES(smiles, title=smiles)
                    self.ligand_oemol_pos_top.append([oemol, positions])
                    self.molecules.append(Molecule.from_openeye(oemol))

                #now make all of the oemol titles 'MOL'
                [self.ligand_oemol_pos_top[i][0].SetTitle("MOL") for i in range(len(self.ligand_oemol_pos_top))]

                #make ligand topologies
                ligand_topologies = [forcefield_generators.generateTopologyFromOEMol(data[0]) for data in self.ligand_oemol_pos_top]

                [self.ligand_oemol_pos_top[i].append(topology) for i, topology in enumerate(ligand_topologies)]

            elif self.ligand_input[-3:] == 'sdf': #
                _logger.info(f"Detected .sdf format.  Proceeding...") #TODO: write checkpoints for sdf format
                oemols = createOEMolFromSDF(self.ligand_input, index = self.ligand_indices)
                positions = [extractPositionsFromOEMol(oemol) for oemol in oemols]
                self.ligand_ffxml = forcefield_generators.generateForceFieldFromMolecules(oemols)
                [oemol.SetTitle("MOL") for oemol in oemols]
                self.smiles_list = [ oechem.OECreateSmiString(oemol, oechem.OESMILESFlag_DEFAULT | oechem.OESMILESFlag_Hydrogens) for oemol in oemols]
                self.ligand_oemol_pos_top = [[oemol, position, forcefield_generators.generateTopologyFromOEMol(oemol)] for oemol, position in zip(oemols, positions)]
                # Create openforcefield Molecule objects
                from openforcefield.topology import Molecule
                self.molecules = [ Molecule.from_openeye(oemol) for oemol in oemols ]

        else:
            raise Exception(f"the ligand input can only be a string pointing to an .sdf or .smi file.  Aborting!")
        self.ligand_md_topologies = [md.Topology.from_openmm(item[2]) for item in self.ligand_oemol_pos_top]

    def _create_proposal_parameters(self, proposal_parameters):
        """
        Define kwargs that will replace proposal_arguments. we are just updating the class attributes

        Arguments
        ---------
        proposal_parameters : dict
            dict of default arguments; must match the same keys of self.proposal_arguments
        """
        if proposal_parameters is not None:
            #first update the __dict__ with kwargs
            #assert that every keyword is in the set of proposal_arguments:
            assert set(proposal_parameters.keys()).issubset(set(self.proposal_arguments.keys())), f"keys: {proposal_parameters.keys()} is not a subset of default argument keys: {self.proposal_arguments.keys()}"
            for keyword in proposal_parameters.keys():
                #assert keyword in proposal_arguments.keys(), f"kwarg keyword {keyword} is not in default argument keys: {proposal_arguments.keys()}"
                assert type(proposal_parameters[keyword]) == type(self.proposal_arguments[keyword]), f"kwarg {keyword}: {proposal_parameters[keyword]} type ({type(proposal_parameters[keyword])}) is not the appropriate type ({type(self.proposal_arguments[keyword])})"

            #specal phasese argument:
            if 'phases' in proposal_parameters.keys():
                assert set(proposal_parameters['phases']).issubset(set(self.known_phases)), f"{set(proposal_parameters['phases'])} is not a subset of known phases: {set(self.known_phases)}.  Aborting!"

            args_left_as_default = set(self.proposal_arguments.keys()).difference(set(proposal_parameters.keys()))

            for arg in args_left_as_default:
                _logger.info(f"{arg} was left as default of {self.proposal_arguments[arg]}")

            self.proposal_arguments.update(proposal_parameters)

        #update the nonbonded method from the default arguments...
        if 'complex' in self.proposal_arguments['phases'] or 'solvent' in self.proposal_arguments['phases']:
            self.nonbonded_method = app.PME
            _logger.info(f"Detected complex or solvent phases: setting PME nonbonded method.")
        elif 'vacuum' in self.proposal_arguments['phases']:
            self.nonbonded_method = app.NoCutoff
            _logger.info(f"Detected vacuum phase: setting noCutoff nonbonded method.")

    def _validate_simulation_parameters(self, simulation_parameters):
        """
        validate simulation_parameters; 'simulation_parameters' accepts many arguments, so we need a separate function to ensure it is handled properly.
        This will create an attribute called simulation_parameters.

        Arguments
        ---------
        simulation_parameters : tuple(str, (dict or None)) or dict, default ('repex', None)
            the simulation parameters to put into the appropriate simulation object
            if type(simulation_parameters) == tuple:
                #then the 0th entry is a string given by 'repex', 'sams', or 'smc', the flavor of simulation
                #and the 1st entry is a dict of parameters that are appropriate to the flavor of simulation
                #if dict is None, then default 'repex' parameters will be used
                in this case, the self.simulation_arguments will be updated appropriately, self.simulation_parameters = None, and self.global_simulation_flavor = simulation_parameters[0]
            elif type(simulation_parameters) == dict, each entry is a {(int, int): {<phase>: (<simulation_flavor>, dict(params))}};
                if an transformation entry is missing, it is defaulted as above.
                in this case, the self.simulation_parameters is set appropriately
                whilst the self.global_simulation_flavor = None


        """
        if type(simulation_parameters) == tuple:
            assert len(simulation_parameters) == 2, f"simulation_parameters is not a tuple with 2 entries"
            assert simulation_parameters[0] in list(self.simulation_arguments.keys()), f"{simulation_parameters[0]} is not a supported "
            assert type(simulation_parameters[1]) in [dict, type(None)], f"the second argument of 'simulation_parameters' must be a dict or None; it is {type(simulation_parameters[1])}"
            if simulation_parameters[1] is None or simulation_parameters[1] == {}:
                _logger.info(f"'simulation_parameters' detected sampler as type '{simulation_parameters[0]}' with default parameters ({simulation_parameters[1]})")
                pass #we do not have to update the simulation_arguments
            else:
                _logger.info(f"'simulation_parameters' detected sampler as type '{simulation_parameters[0]}' with non-default parameters")
                assert set(simulation_parameters[1].keys()).issubset(set(self.simulation_arguments[simulation_parameters[0]].keys())), f"there are extra arguments in 'simulation_parameters': {set(simulation_parameters[1].keys()).difference(set(self.simulation_arguments[simulation_parameters[0]].keys()))}"
                args_left_as_default = set(self.simulation_arguments[simulation_parameters[0]].keys()).difference(set(simulation_parameters[1].keys()))
                for arg in args_left_as_default:
                    _logger.info(f"{arg} was left as default of {self.simulation_arguments[simulation_parameters[0]][arg]}")
                for keyword in simulation_parameters[1].keys():
                    #assert keyword in proposal_arguments.keys(), f"kwarg keyword {keyword} is not in default argument keys: {proposal_arguments.keys()}"
                    assert type(simulation_parameters[1][keyword]) == type(self.simulation_arguments[simulation_parameters[0]][keyword]), f"{keyword} type ({type(simulation_parameters[keyword])}) is not supported"

                #update the simulation_arguments:
                self.simulation_arguments[simulation_parameters[0]].update(simulation_parameters[1])

            #now we can set a global simulation flavor and set the simulation parameters to None
            self.global_simulation_flavor = simulation_parameters[0]
            self.simulation_parameters = None

        elif type(simulation_parameters) == dict:
            _logger.info(f"'simulation_parameters' detected dict as argument")
            assert max(np.array(list(simulation_parameters.keys())).flatten()) < self.adjacency_matrix.shape[0], f"the maximal simulation_parameters entry does not live in the adjacency matrix shape"
            assert all(type(entry) == dict for entry in simulation_parameters.values()), f"each value of 'simulation_parameters' must be a dict"
            self.simulation_parameter_assertions(simulation_parameters)
            self.simulation_parameters = copy.deepcopy(simulation_parameters)
            self.global_simulation_flavor = None
            for index, entry in simulation_parameters.items():
                for phase, tup in entry.items():
                    sim_flavor, args = tup[0], tup[1]
                    args_left_as_default = set(self.simulation_arguments[sim_flavor].keys()).difference(set(args.keys()))
                    interim_dict = {arg: self.simulation_arguments[sim_flavor][arg] for arg in args_left_as_default}
                    for _arg in args_left_as_default:
                        self.simulation_parameters[index][phase][1].update(interim_dict)
                        #note we only make simulation parameters attribute if the argument simulation parameters is a np.ndarray
        else:
            raise Exception(f"simulation_parameters type {type(simulation_parameters)} is not currently supported.  Aborting")

    def simulation_parameter_assertions(self, _dict):
        """
        validates the input of the simulation_parameters as a dict

        Arguments
        ---------
        _dict : dict
            the dict that will be validated
        """
        for entry in _dict.values():
            assert set(entry.keys()).issubset(set(self.proposal_arguments['phases'])), f"the phases in 'simulation_parameters' do not agree with 'proposal_arguments'"
            assert all(type(val) == tuple and len(val) == 2 for val in entry.values()), f"the values of the 'simulation_parameters' value values must be tuples of length 2"
            for tup in entry.values():
                assert tup[0] in list(self.simulation_arguments.keys()), f"the simulation flavor is not supported"
                assert set(tup[1].keys()).issubset(set(self.simulation_arguments[tup[0]].keys())), f"the simulation parameters are not a subset of the allowable simulation parameters for simulation type {tup[0]}"
                for keyword, arg in tup[1].items():
                    assert type(arg) == type(self.simulation_arguments[tup[0]][keyword]), f"keyword '{keyword}' type '{type(arg)}' is not supported"


    def _create_system_generator(self):
        """
        Wrap the process for generating a dict of system generators for each phase.
        """
        from openmmforcefields.generators import SystemGenerator

        barostat = None
        if self.proposal_arguments['pressure'] is not None:
            if self.nonbonded_method == app.PME:
                barostat = openmm.MonteCarloBarostat(self.proposal_arguments['pressure'],
                                                     self.proposal_arguments['temperature'],
                                                     50)

        forcefield_kwargs = {'removeCMMotion': False,
                           'nonbondedMethod': self.nonbonded_method,
                           'constraints' : app.HBonds,
                           'hydrogenMass' : self.proposal_arguments['hmass']}

        self.system_generator = SystemGenerator(forcefields=self.proposal_arguments['forcefield_files'],
                                                small_molecule_forcefield=self.proposal_arguments['small_molecule_forcefield'],
                                                cache=self.proposal_arguments['small_molecule_parameters_cache'],
                                                molecules=self.molecules,
                                                barostat=barostat,
                                                forcefield_kwargs=forcefield_kwargs)

    def _setup_complex_phase(self, ligand_oemol, ligand_positions, ligand_topology):
        """
        Creates complex positions and topology given ligand positions and topology.

        Arguments
        ---------
        ligand_oemol : oechem.oemol object
            oemol of ligand (this is only necessary for .sdf-type receptor files)
        ligand_positions : unit.Quantity(np.ndarray(), units = units.nanometers)
            positions of the ligand
        ligand_topology : mdtraj.Topology
            md topology of the ligand

        Returns
        -------
        complex_md_topology : mdtraj.Topology
            complex mdtraj topology
        complex_topology : simtk.openmm.Topology
            complex openmm topology
        complex_positions : unit.Quantity(np.ndarray(), units = units.nanometers)
            positions of the complex
        """
        if self.receptor_filename[-3:] == 'pdb':
            with open(self.receptor_filename, 'r') as pdbfile:
                receptor_pdb = app.PDBFile(pdbfile)
            receptor_positions = receptor_pdb.positions
            receptor_topology = receptor_pdb.topology
            receptor_mdtraj_topology = md.Topology.from_openmm(receptor_topology)

        elif self.receptor_filename[:-4] == 'mol2':
            receptor_mol = createOEMolFromSDF(self.receptor_filename)
            receptor_positions = extractPositionsFromOEMol(receptor_mol)
            receptor_topology = self._receptor_topology_old = forcefield_generators.generateTopologyFromOEMol(receptor_mol)
            receptor_mdtraj_topology = md.Topology.from_openmm(receptor_topology)

        complex_md_topology = receptor_mdtraj_topology.join(ligand_topology)
        complex_topology = complex_md_topology.to_openmm()
        n_atoms_complex = complex_topology.getNumAtoms()
        n_atoms_receptor = receptor_topology.getNumAtoms()

        complex_positions = unit.Quantity(np.zeros([n_atoms_complex, 3]), unit=unit.nanometers)
        complex_positions[:n_atoms_receptor, :] = receptor_positions
        complex_positions[n_atoms_receptor:, :] = ligand_positions

        return complex_md_topology, complex_topology, complex_positions

    def _solvate(self, topology, positions, model = 'tip3p', vacuum = False):
        """
        solvate a topology, position and return a topology, position, and system

        Argumnts
        --------
        topology : simtk.openmm.Topology
            topology of the object to be solvated
        positions : unit.Quantity(np.ndarray(), units = units.nanometers)
            positions of the complex
        model : str, default 'tip3p'
            solvent model to use for solvation
        vacuum : bool, default False
            whether to prepare system in vacuum

        Returns
        -------
        solvated_topology : app.Topology
            Topology of the system with added waters
        solvated_positions : [n + 3(n_waters), 3] ndarray of Quantity nm
            Solvated positions
        solvated_system : openmm.System
            The parameterized system, containing a barostat if one was specified.
        """
        modeller = app.Modeller(topology, positions)
        hs = [atom for atom in modeller.topology.atoms() if atom.element.symbol in ['H'] and atom.residue.name not in ['MOL','OLD','NEW']]
        modeller.delete(hs)
        modeller.addHydrogens(forcefield = self.system_generator.forcefield)
        if not vacuum:
            _logger.info(f"\tpreparing to add solvent")
            modeller.addSolvent(self.system_generator.forcefield,
                                model=model,
                                padding = self.proposal_arguments['solvent_padding'],
                                ionicStrength = 0.15*unit.molar)
        else:
            _logger.info(f"\tSkipping solvation of vacuum perturbation")
        solvated_topology = modeller.getTopology()
        solvated_positions = modeller.getPositions()

        solvated_positions = unit.quantity.Quantity(value = np.array([list(atom_pos) for atom_pos in solvated_positions.value_in_unit_system(unit.md_unit_system)]), unit = unit.nanometers)
        solvated_system = self.system_generator.create_system(solvated_topology)
        return solvated_topology, solvated_positions, solvated_system

    def _generate_solvent_topologies(self, topology_proposal, old_positions):
        """
        This method generates ligand-only topologies and positions from a TopologyProposal containing a solvated complex.
        The output of this method is then used when building the solvent-phase simulation with the same atom map.

        Parameters
        ----------
        topology_proposal : perses.rjmc.topology_proposal.TopologyProposal
            Topology proposal object of the ligand in complex
        old_positions : array
            Positions of the fully solvated complex

        Returns
        -------
        ligand_topology_proposal : perses.rjmc.topology_proposal.TopologyProposal
            Topology proposal object of the ligand without complex

        old_solvated_positions : array
            positions of the system without complex
        """
        old_complex = md.Topology.from_openmm(topology_proposal.old_topology)
        new_complex = md.Topology.from_openmm(topology_proposal.new_topology)

        atom_map = topology_proposal.old_to_new_atom_map

        old_mol_start_index, old_mol_len = self.proposal_engine._find_mol_start_index(old_complex.to_openmm())
        new_mol_start_index, new_mol_len = self.proposal_engine._find_mol_start_index(new_complex.to_openmm())

        old_pos = unit.Quantity(np.zeros([len(old_positions), 3]), unit=unit.nanometers)
        old_pos[:, :] = old_positions
        old_ligand_positions = old_pos[old_mol_start_index:(old_mol_start_index + old_mol_len), :]

        # subset the topologies:
        old_ligand_topology = old_complex.subset(old_complex.select("resname == 'MOL' "))
        new_ligand_topology = new_complex.subset(new_complex.select("resname == 'MOL' "))

        # solvate the old ligand topology:
        old_solvated_topology, old_solvated_positions, old_solvated_system = self._solvate(old_ligand_topology.to_openmm(), old_ligand_positions)

        old_solvated_md_topology = md.Topology.from_openmm(old_solvated_topology)

        # now remove the old ligand, leaving only the solvent
        solvent_only_topology = old_solvated_md_topology.subset(old_solvated_md_topology.select("not resname MOL"))
        # append the solvent to the new ligand-only topology:
        new_solvated_ligand_md_topology = new_ligand_topology.join(solvent_only_topology)
        nsl, b = new_solvated_ligand_md_topology.to_dataframe()

        # dirty hack because new_solvated_ligand_md_topology.to_openmm() was throwing bond topology error
        new_solvated_ligand_md_topology = md.Topology.from_dataframe(nsl, b)

        new_solvated_ligand_omm_topology = new_solvated_ligand_md_topology.to_openmm()
        new_solvated_ligand_omm_topology.setPeriodicBoxVectors(old_solvated_topology.getPeriodicBoxVectors())

        # create the new ligand system:
        new_solvated_system = self.system_generator.create_system(new_solvated_ligand_omm_topology)

        new_to_old_atom_map = {atom_map[x] - new_mol_start_index: x - old_mol_start_index for x in
                               old_complex.select("resname == 'MOL' ") if x in atom_map.keys()}

        # adjust the atom map to account for the presence of solvent degrees of freedom:
        # By design, all atoms after the ligands are water, and should be mapped.
        n_water_atoms = solvent_only_topology.to_openmm().getNumAtoms()
        for i in range(n_water_atoms):
            new_to_old_atom_map[new_mol_len + i] = old_mol_len + i

        # make a TopologyProposal
        ligand_topology_proposal = TopologyProposal(new_topology=new_solvated_ligand_omm_topology,
                                                    new_system=new_solvated_system,
                                                    old_topology=old_solvated_topology, old_system=old_solvated_system,
                                                    new_to_old_atom_map=new_to_old_atom_map, old_chemical_state_key='A',
                                                    new_chemical_state_key='B')

        return ligand_topology_proposal, old_solvated_positions

    def _generate_vacuum_topologies(self, topology_proposal, old_positions, system_generator):
        """
        This method generates ligand-only topologies and positions from a TopologyProposal containing a solvated complex.
        The output of this method is then used when building the solvent-phase simulation with the same atom map.

        Parameters
        ----------
        topology_proposal : perses.rjmc.topology_proposal.TopologyProposal
            topology proposal to parse
        old_positions : array
            Positions of the fully solvated protein ligand syste
        system_generator : perses.rjmc.TopologyProposal.SystemGenerator
            the system generator used to create the system

        Returns
        -------
        ligand_topology_proposal : perses.rjmc.topology_proposal.TopologyProposal
            Topology proposal object of the ligand without complex

        old_solvated_positions : array
            positions of the system without complex
        """
        old_complex = md.Topology.from_openmm(topology_proposal.old_topology)
        new_complex = md.Topology.from_openmm(topology_proposal.new_topology)

        atom_map = topology_proposal.old_to_new_atom_map

        old_mol_start_index, old_mol_len = self.proposal_engine._find_mol_start_index(old_complex.to_openmm())
        new_mol_start_index, new_mol_len = self.proposal_engine._find_mol_start_index(new_complex.to_openmm())

        old_pos = unit.Quantity(np.zeros([len(old_positions), 3]), unit=unit.nanometers)
        old_pos[:, :] = old_positions
        old_ligand_positions = old_pos[old_mol_start_index:(old_mol_start_index + old_mol_len), :]

        # subset the topologies:
        old_ligand_topology = old_complex.subset(old_complex.select("resname == 'MOL' "))
        new_ligand_topology = new_complex.subset(new_complex.select("resname == 'MOL' "))

        # convert to openmm topology object
        old_ligand_topology = old_ligand_topology.to_openmm()
        new_ligand_topology = new_ligand_topology.to_openmm()

        # create the new ligand system:
        old_ligand_system = system_generator.create_system(old_ligand_topology)
        new_ligand_system = system_generator.create_system(new_ligand_topology)

        new_to_old_atom_map = {atom_map[x] - new_mol_start_index: x - old_mol_start_index for x in
                               old_complex.select("resname == 'MOL' ") if x in atom_map.keys()}


        # make a TopologyProposal
        ligand_topology_proposal = TopologyProposal(new_topology=new_ligand_topology,
                                                    new_system=new_ligand_system,
                                                    old_topology=old_ligand_topology, old_system=old_ligand_system,
                                                    new_to_old_atom_map=new_to_old_atom_map, old_chemical_state_key='A',
                                                    new_chemical_state_key='B')

        return ligand_topology_proposal, old_ligand_positions

    def _handle_valence_energies(self, topology_proposal):
        """
        simple wrapper function to return forward and reverse valence energies from the complex proposal

        Arguments
        ---------
        topology_proposal : perses.rjmc.topology_proposal.TopologyProposal
            topology proposal to parse

        Returns
        -------
        added_valence_energy : float
            the reduced valence energy pulled from geometry_engine forward
        subtracted_valence_energy : float
            the reduced valence energy pulled from geometry_engine reverse
        """
        if not topology_proposal.unique_new_atoms:
            assert self.geometry_engine.forward_final_context_reduced_potential == None, f"There are no unique new atoms but the geometry_engine's final context reduced potential is not None (i.e. {self.geometry_engine.forward_final_context_reduced_potential})"
            assert self.geometry_engine.forward_atoms_with_positions_reduced_potential == None, f"There are no unique new atoms but the geometry_engine's forward atoms-with-positions-reduced-potential in not None (i.e. { self.geometry_engine.forward_atoms_with_positions_reduced_potential})"
            added_valence_energy = 0.0
        else:
            added_valence_energy = self.geometry_engine.forward_final_context_reduced_potential - self.geometry_engine.forward_atoms_with_positions_reduced_potential

        if not topology_proposal.unique_old_atoms:
            assert self.geometry_engine.reverse_final_context_reduced_potential == None, f"There are no unique old atoms but the geometry_engine's final context reduced potential is not None (i.e. {self.geometry_engine.reverse_final_context_reduced_potential})"
            assert self.geometry_engine.reverse_atoms_with_positions_reduced_potential == None, f"There are no unique old atoms but the geometry_engine's atoms-with-positions-reduced-potential in not None (i.e. { self.geometry_engine.reverse_atoms_with_positions_reduced_potential})"
            subtracted_valence_energy = 0.0
        else:
            subtracted_valence_energy = self.geometry_engine.reverse_final_context_reduced_potential - self.geometry_engine.reverse_atoms_with_positions_reduced_potential

        return added_valence_energy, subtracted_valence_energy


    def _generate_proposals(self,
                            current_oemol,
                            proposed_oemol,
                            current_positions,
                            current_topology):
        """
        Create topology and geometry proposals for a ligand in every specified phase.
        If complex is specified, the topology proposal is recycled for all phases; else, the proposal is conducted
        in solvent and recycled for vacuum.  If the only phase is vacuum, then we generate a single proposal without recycling.

        This method requires some refactoring...perhaps use classes to generate the phases...

        Arguments
        ---------

        """
        proposals = {}
        if 'complex' in self.proposal_arguments['phases']:
            _logger.debug(f"\t\tcomplex:")
            assert self.nonbonded_method == app.PME, f"Complex phase is specified, but the nonbonded method is not {app.PME} (is currently {self.nonbonded_method})."
            complex_md_topology, complex_topology, complex_positions = self._setup_complex_phase(ligand_oemol = current_oemol,
                                                                                                 ligand_positions = current_positions,
                                                                                                 ligand_topology = md.Topology.from_openmm(current_topology))

            solvated_complex_topology, solvated_complex_positions, solvated_complex_system = self._solvate(complex_topology,
                                                                                                           complex_positions,
                                                                                                           model = self.proposal_arguments['water_model'],
                                                                                                           vacuum = False)
            solvated_complex_md_topology = md.Topology.from_openmm(solvated_complex_topology)
            complex_topology_proposal = self.proposal_engine.propose(current_system = solvated_complex_system,
                                                                     current_topology = solvated_complex_topology,
                                                                     current_mol = current_oemol,
                                                                     proposed_mol = proposed_oemol)

            proposed_solvated_complex_positions, complex_logp_proposal = self.geometry_engine.propose(complex_topology_proposal,
                                                                                                      solvated_complex_positions,
                                                                                                      self.beta)
            complex_logp_reverse = self.geometry_engine.logp_reverse(complex_topology_proposal,
                                                             proposed_solvated_complex_positions,
                                                             solvated_complex_positions, self.beta)

            complex_added_valence_energy, complex_subtracted_valence_energy = self._handle_valence_energies(complex_topology_proposal)
            complex_forward_neglected_angles = self.geometry_engine.forward_neglected_angle_terms
            complex_reverse_neglected_angles = self.geometry_engine.reverse_neglected_angle_terms

            #now to add it to phases
            proposals.update({'complex': {'topology_proposal': complex_topology_proposal,
                                          'current_positions': solvated_complex_positions,
                                          'proposed_positions': proposed_solvated_complex_positions,
                                          'logp_proposal': complex_logp_proposal,
                                          'logp_reverse': complex_logp_reverse,
                                          'added_valence_energy': complex_added_valence_energy,
                                          'subtracted_valence_energy': complex_subtracted_valence_energy,
                                          'forward_neglected_angles': complex_forward_neglected_angles,
                                          'reverse_neglected_angles': complex_reverse_neglected_angles}
                                          })

        if 'solvent' in self.proposal_arguments['phases']:
            _logger.debug(f"\t\tsolvent:")
            if 'complex' in self.proposal_arguments['phases']:
                assert 'complex' in proposals.keys(), f"'complex' is a phase that should have been handled, but it is not in proposals."
                assert self.nonbonded_method == app.PME, f"solvent phase is specified, but the nonbonded method is not {app.PME} (is currently {self.nonbonded_method})."
                solvated_ligand_topology_proposal, solvated_ligand_positions = self._generate_solvent_topologies(topology_proposal = complex_topology_proposal,
                                                                                                                 old_positions = solvated_complex_positions)
            else:
                solvated_ligand_topology, solvated_ligand_positions, solvated_ligand_system = self._solvate(current_topology,
                                                                                                            current_positions,
                                                                                                            model = self.proposal_arguments['water_model'],
                                                                                                            vacuum = False)
                solvated_ligand_md_topology = md.Topology.from_openmm(solvated_ligand_topology)
                solvated_ligand_topology_proposal = self.proposal_engine.propose(current_system = solvated_ligand_system,
                                                                                 current_topology = solvated_ligand_topology,
                                                                                 current_mol = current_oemol,
                                                                                 proposed_mol = proposed_oemol)
            proposed_solvated_ligand_positions, solvent_logp_proposal = self.geometry_engine.propose(solvated_ligand_topology_proposal,
                                                                                             solvated_ligand_positions,
                                                                                             self.beta)
            solvent_logp_reverse = self.geometry_engine.logp_reverse(solvated_ligand_topology_proposal,
                                                                     proposed_solvated_ligand_positions,
                                                                     solvated_ligand_positions,
                                                                     self.beta)

            solvated_added_valence_energy, solvated_subtracted_valence_energy = self._handle_valence_energies(solvated_ligand_topology_proposal)
            solvated_forward_neglected_angles = self.geometry_engine.forward_neglected_angle_terms
            solvated_reverse_neglected_angles = self.geometry_engine.reverse_neglected_angle_terms

            #now to add it to phases
            proposals.update({'solvent': {'topology_proposal': solvated_ligand_topology_proposal,
                                          'current_positions': solvated_ligand_positions,
                                          'proposed_positions': proposed_solvated_ligand_positions,
                                          'logp_proposal': solvent_logp_proposal,
                                          'logp_reverse': solvent_logp_reverse,
                                          'added_valence_energy': solvated_added_valence_energy,
                                          'subtracted_valence_energy': solvated_subtracted_valence_energy,
                                          'forward_neglected_angles': solvated_forward_neglected_angles,
                                          'reverse_neglected_angles': solvated_reverse_neglected_angles}
                                          })

        if 'vacuum' in self.proposal_arguments['phases']:
            _logger.debug(f"\t\tvacuum:")
            # Create modified SystemGenerator for vacuum systems
            # TODO: Should we instead allow SystemGenerator.create_system() to override kwargs?
            #       Or perhaps it should recognize whether the Topology object is periodic or not?
            vacuum_system_generator = copy.deepcopy(self.system_generator)
            vacuum_system_generator.forcefield_kwargs['nonbondedMethod'] = app.NoCutoff

            if 'complex' not in self.proposal_arguments['phases'] and 'solvent' not in self.proposal_arguments['phases']:
                vacuum_ligand_topology, vacuum_ligand_positions, vacuum_ligand_system = self._solvate(current_topology,
                                                                                                      current_positions,
                                                                                                      vacuum=True)
                vacuum_ligand_topology_proposal = self.proposal_engine.propose(vacuum_ligand_system,
                                                                               vacuum_ligand_topology,
                                                                               current_mol = current_oemol,
                                                                               proposed_mol = proposed_oemol)
            elif 'complex' in self.proposal_arguments['phases']:
                vacuum_ligand_topology_proposal, vacuum_ligand_positions = self._generate_vacuum_topologies(complex_topology_proposal,
                                                                                                            solvated_complex_positions,
                                                                                                            vacuum_system_generator)
            elif 'solvent' in self.proposal_arguments['phases']:
                vacuum_ligand_topology_proposal, vacuum_ligand_positions = self._generate_vacuum_topologies(solvated_ligand_topology_proposal,
                                                                                                     solvated_ligand_positions,
                                                                                                     vacuum_system_generator)
            else:
                raise Exeption(f"There is an unnacounted for error in the topology proposal generation for vacuum phase.  Aborting!")

            proposed_vacuum_ligand_positions, vacuum_logp_proposal = self.geometry_engine.propose(vacuum_ligand_topology_proposal,
                                                                                                  vacuum_ligand_positions,
                                                                                                  self.beta)
            vacuum_logp_reverse = self.geometry_engine.logp_reverse(vacuum_ligand_topology_proposal,
                                                                    proposed_vacuum_ligand_positions,
                                                                    vacuum_ligand_positions,
                                                                    self.beta)

            vacuum_added_valence_energy, vacuum_subtracted_valence_energy = self._handle_valence_energies(vacuum_ligand_topology_proposal)
            vacuum_forward_neglected_angles = self.geometry_engine.forward_neglected_angle_terms
            vacuum_reverse_neglected_angles = self.geometry_engine.reverse_neglected_angle_terms

            #now to add it to phases
            proposals.update({'vacuum': {'topology_proposal': vacuum_ligand_topology_proposal,
                                          'current_positions': vacuum_ligand_positions,
                                          'proposed_positions': proposed_vacuum_ligand_positions,
                                          'logp_proposal': vacuum_logp_proposal,
                                          'logp_reverse': vacuum_logp_reverse,
                                          'added_valence_energy': vacuum_added_valence_energy,
                                          'subtracted_valence_energy': vacuum_subtracted_valence_energy,
                                          'forward_neglected_angles': vacuum_forward_neglected_angles,
                                          'reverse_neglected_angles': vacuum_reverse_neglected_angles}
                                          })

        return proposals

class RelativeTransformationPhase():
    """
    Base class for generating different phases of relative calculations
    """
    def __init__(self,
                 system_generator,
                 proposal_engine,
                 nonbonded_method
                 ):
        """
        Define miscellaneous attributes needed for all phase transforms
        """
    def run_setup(self, topology_proposal = None):
        """
        Wrapper method for setup generation for all phases
        """
        if topology_proposal is None:
            self._run_topology_setup(**kwargs)
        else:
            self._run_normal_setup(**kwargs)

    def _run_normal_setup(self):
        old_ligand_topology, new_ligand_topology, old_ligand_positions = self._extract_from_topology_proposal(topology_proposal, old_topology, new_topology, old_positions)
        returnable_dict = self._run_topology_setup()
        return returnable_dict


    def _solvate(self, topology, positions, model = 'tip3p', vacuum = False):
        """
        solvate a topology, position and return a topology, position, and system

        Argumnts
        --------
        topology : simtk.openmm.Topology
            topology of the object to be solvated
        positions : unit.Quantity(np.ndarray(), units = units.nanometers)
            positions of the complex
        model : str, default 'tip3p'
            solvent model to use for solvation
        vacuum : bool, default False
            whether to prepare system in vacuum

        Returns
        -------
        solvated_topology : app.Topology
            Topology of the system with added waters
        solvated_positions : [n + 3(n_waters), 3] ndarray of Quantity nm
            Solvated positions
        solvated_system : openmm.System
            The parameterized system, containing a barostat if one was specified.
        """
        modeller = app.Modeller(topology, positions)
        if not vacuum:
            _logger.info(f"\tpreparing to add solvent")
            modeller.addSolvent(self.system_generator.forcefield,
                                model=model,
                                padding = self.proposal_arguments['solvent_padding'],
                                ionicStrength = 0.15*unit.molar)
        else:
            _logger.info(f"\tSkipping solvation of vacuum perturbation")
        solvated_topology = modeller.getTopology()
        solvated_positions = modeller.getPositions()

        solvated_positions = unit.quantity.Quantity(value = np.array([list(atom_pos) for atom_pos in solvated_positions.value_in_unit_system(unit.md_unit_system)]), unit = unit.nanometers)
        solvated_system = self.system_generator.create_system(solvated_topology)
        return solvated_topology, solvated_positions, solvated_system

    def _extract_from_topology_proposal(topology_proposal, old_positions):
        old_md_topology = md.Topology.from_openmm(topology_proposal.old_topology)
        new_md_topology = md.Topology.from_openmm(topology_proposal.new_topology)

        atom_map = topology_proposal.old_to_new_atom_map

        old_mol_start_index, old_mol_len = self.proposal_engine._find_mol_start_index(old_md_topology.to_openmm())
        new_mol_start_index, new_mol_len = self.proposal_engine._find_mol_start_index(new_md_topology.to_openmm())

        old_pos = unit.Quantity(np.zeros([len(old_positions), 3]), unit=unit.nanometers)
        old_pos[:, :] = old_positions
        old_ligand_positions = old_pos[old_mol_start_index:(old_mol_start_index + old_mol_len), :]

        # subset the topologies:
        old_ligand_topology = old_md_topology.subset(old_md_topology.select("resname == 'MOL' ")).to_openmm()
        new_ligand_topology = new_md_topology.subset(new_md_topology.select("resname == 'MOL' ")).to_openmm()

        return old_ligand_topology, new_ligand_topology, old_ligand_positions

    def _wrap_topology_and_geometry_proposals():
        complex_topology_proposal = self.proposal_engine.propose(current_system = solvated_complex_system,
                                                                 current_topology = solvated_complex_topology,
                                                                 current_mol = current_oemol,
                                                                 proposed_mol = proposed_oemol)

        proposed_solvated_complex_positions, complex_logp_proposal = self.geometry_engine.propose(complex_topology_proposal,
                                                                                                  solvated_complex_positions,
                                                                                                  self.beta)
        complex_logp_reverse = self.geometry_engine.logp_reverse(complex_topology_proposal,
                                                         proposed_solvated_complex_positions,
                                                         solvated_complex_positions, self.beta)

        complex_added_valence_energy, complex_subtracted_valence_energy = self._handle_valence_energies(complex_topology_proposal)
        complex_forward_neglected_angles = self.geometry_engine.forward_neglected_angle_terms
        complex_reverse_neglected_angles = self.geometry_engine.reverse_neglected_angle_terms

        returnable_dict = {'topology_proposal': complex_topology_proposal,
                           'current_positions': solvated_complex_positions,
                           'proposed_positions': proposed_solvated_complex_positions,
                           'logp_proposal': complex_logp_proposal,
                           'logp_reverse': complex_logp_reverse,
                           'added_valence_energy': complex_added_valence_energy,
                           'subtracted_valence_energy': complex_subtracted_valence_energy,
                           'forward_neglected_angles': complex_forward_neglected_angles,
                           'reverse_neglected_angles': complex_reverse_neglected_angles}

        return returnable_dict







class ComplexTransformationPhase(RelativeTransformationPhase):
    """
    Complex specific transformation subclass
    """
    def __init__(self,
                 system_generator):
        """
        First instantiate the RelativeTransformationPhase super class
        """
        super(ComplexTransformationPhase, self).__init__(system_generator)

    def _run_topology_setup():

        #setup complex phase
        complex_md_topology, complex_topology, complex_positions = self._setup_complex_phase(ligand_oemol = current_oemol,
                                                                                             ligand_positions = current_positions,
                                                                                             ligand_topology = md.Topology.from_openmm(current_topology))

        solvated_complex_topology, solvated_complex_positions, solvated_complex_system = self._solvate(complex_topology,
                                                                                                       complex_positions,
                                                                                                       model = self.proposal_arguments['water_model'],
                                                                                                       vacuum = False)
        solvated_complex_md_topology = md.Topology.from_openmm(solvated_complex_topology)

        returnable_dict = self._wrap_topology_and_geometry_proposals()
        return returnable_dict

        def _run_normal_setup():
            old_ligand_topology, new_ligand_topology, old_ligand_positions = _extract_from_topology_proposal(topology_proposal, old_topology, new_topology, old_positions)
            returnable_dict = _run_topology_setup()
            return returnable_dict

    def _setup_complex_phase(self, ligand_oemol, ligand_positions, ligand_topology):
        """
        Creates complex positions and topology given ligand positions and topology.

        Arguments
        ---------
        ligand_oemol : oechem.oemol object
            oemol of ligand (this is only necessary for .sdf-type receptor files)
        ligand_positions : unit.Quantity(np.ndarray(), units = units.nanometers)
            positions of the ligand
        ligand_topology : mdtraj.Topology
            md topology of the ligand

        Returns
        -------
        complex_md_topology : mdtraj.Topology
            complex mdtraj topology
        complex_topology : simtk.openmm.Topology
            complex openmm topology
        complex_positions : unit.Quantity(np.ndarray(), units = units.nanometers)
            positions of the complex
        """
        if self.receptor_filename[-3:] == 'pdb':
            with open(self.receptor_filename, 'r') as pdbfile:
                receptor_pdb = app.PDBFile(pdbfile)
            receptor_positions = receptor_pdb.positions
            receptor_topology = receptor_pdb.topology
            receptor_mdtraj_topology = md.Topology.from_openmm(receptor_topology)

        elif self.receptor_filename[:-4] == 'mol2':
            receptor_mol = createOEMolFromSDF(self.receptor_filename)
            receptor_positions = extractPositionsFromOEMol(receptor_mol)
            receptor_topology = self._receptor_topology_old = forcefield_generators.generateTopologyFromOEMol(receptor_mol)
            receptor_mdtraj_topology = md.Topology.from_openmm(receptor_topology)

        complex_md_topology = receptor_mdtraj_topology.join(ligand_topology)
        complex_topology = complex_md_topology.to_openmm()
        n_atoms_complex = complex_topology.getNumAtoms()
        n_atoms_receptor = receptor_topology.getNumAtoms()

        complex_positions = unit.Quantity(np.zeros([n_atoms_complex, 3]), unit=unit.nanometers)
        complex_positions[:n_atoms_receptor, :] = receptor_positions
        complex_positions[n_atoms_receptor:, :] = ligand_positions

        return complex_md_topology, complex_topology, complex_positions



class ExplicitWaterTransformationPhase(RelativeTransformationPhase):
    """
    Complex specific transformation subclass
    """
    def __init__(self,
                 system_generator):
        """
        First instantiate the RelativeTransformationPhase super class
        """
        super(ExplicitWaterTransformationPhase, self).__init__(system_generator)

    def _run_topology_setup():
        solvated_ligand_topology, solvated_ligand_positions, solvated_ligand_system = self._solvate(current_topology,
                                                                                                    current_positions,
                                                                                                    model = self.proposal_arguments['water_model'],
                                                                                                    vacuum = False)
        returnable_dict = self._wrap_topology_and_geometry_proposals()
        return returnable_dict

    def _run_normal_setup():







class Experiment():
    """
    Main class with which to conduct computation on a network of ligands/protein mutations in different phases.
    """

    def __init__(self,
                 #NetworkBuilder params
                 ligand_input,
                 ligand_indices = None,
                 receptor_filename = None,
                 graph_connectivity = 'fully_connected',
                 proposal_parameters = None,
                 simulation_parameters = ('repex', None),

                 #setup resources
                 setup_client_num_workers = 10,
                 resources = None):
        """
        Initialization method will construct a network on which to run a computation.

        Arguments
        ----------
        ligand_input : str
            the name of the ligand file (any openeye supported format)
            this can either be an .sdf or list of .sdf files, or a list of SMILES strings

        ligand_indices : list of int
            indices in the ligand input to parse

        receptor_filename : str, default None
            Receptor mol2 or pdb filename. If None, complex leg will be omitted.

        graph_connectivity : str or np.matrix or list of tuples, default 'fully_connected'
            The graph connectivity information for the experiment.  This accepts one of several allowable
            strings (corresponding to connectivity defaults), a 2d np.array specifying the explicit connectivity
            matrix for indexed ligands, or a list of tuples corresponding to pairwise ligand index connections.
            The default is 'fully_connected', which specified a fully connected (i.e. complete or single-clique) graph.
            If the graph_connectivity is input as a 2d numpy array, values represent the weights of the transform.  0 weights entail no connectivity.
            The self.adjacency_matrix argument produced as a result contains the log(weights) of the transform as entries.

        client : generalized client object, default None
            generalized client object from which workers can be called to perform edge computations

        proposal_parameters: dict, default None
            The following dict is parseable from a setup yaml, but have defaults given if no setup yaml is given.
            They mostly consist of the thermodynamic state of the graph, several potential modifications to the
            hybrid factory (i.e. the alchemical system), the single-topology mapping criteria, and the sampler-specific
            parameters (i.e. parameters specific to Replica Exchange, SAMS, and sMC.)

            forcefield_files : list of str
                The list of ffxml files that contain the forcefields that will be used
            pressure : Quantity, units of pressure
                Pressure to use in the barostat
            temperature : Quantity, units of temperature
                Temperature to use for the Langevin integrator
            solvent_padding : Quantity, units of length
                The amount of padding to use when adding solvent
            neglect_angles : bool
                Whether to neglect certain angle terms for the purpose of minimizing work variance in the RJMC protocol.
            anneal_14s : bool, default False
                Whether to anneal 1,4 interactions over the protocol;
                    if True, then geometry_engine takes the argument use_14_nonbondeds = False;
                    if False, then geometry_engine takes the argument use_14_nonbondeds = True;

        simulation_parameters : tuple(str, (dict or None)) or dict, default ('repex', None)
            the simulation parameters to put into the appropriate simulation object
            if type(simulation_parameters) == tuple:
                #then the 0th entry is a string given by 'repex', 'sams', or 'smc', the flavor of simulation
                #and the 1st entry is a dict of parameters that are appropriate to the flavor of simulation
                #if dict is None, then default 'repex' parameters will be used
            elif type(simulation_parameters) == dict, each entry is a {(int, int): {<phase>: (<simulation_flavor>, dict(params))}};
                if an transformation entry is missing, it is defaulted as above

        setup_client_num_workers : int
            the number of workers that will be used to build the network; this computational cost is separate
            from `resources`

        resources : dict, default None
            dictionary specifying the following attributes
                1. number of gpus, cpus
                2. RAM per gpu/cpu
                3. total run_time (hours) of gpus/cpus
            format: {
                    'gpus': {'number': int or None, 'RAM': int, 'runtime': float},
                    'cpus': {'number': int or None, 'RAM': int, 'runtime': float}
                    }
        """
        #the first thing we need to do is to build the parallelism/scheduler
        #the setup client will be built only from cpus
        setup_parallelism = Parallelism()
        setup_parallelism.activate_client(library = ('dask', 'LSF'),
                                          num_processes = setup_client_num_workers,
                                          timeout = 1800,
                                          processor = 'cpu')
