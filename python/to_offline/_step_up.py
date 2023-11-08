from horizon.rhc.model_description import FullModelInverseDynamics
from horizon.utils import utils as horizon_utils
from horizon.utils import kin_dyn
from horizon.solvers import Solver
from horizon.problem import Problem
from casadi_kin_dyn import pycasadi_kin_dyn
from horizon.transcriptions.transcriptor import Transcriptor
from horizon.utils.patternGenerator import PatternGenerator
from horizon.utils.trajectoryGenerator import TrajectoryGenerator
import time
import marker_spawner
import multiprocessing
import functools
import numpy as np


def run(q_init,
        base_init,
        contacts,
        solver_type,
        kd,
        transcription_method,
        transcription_opts=None,
        flag_upper_body=True,
        kd_frame=pycasadi_kin_dyn.CasadiKinDyn.LOCAL_WORLD_ALIGNED,
        ):
    ns = 125  # trot
    tf = 10.  # 10s

    dt = tf / ns
    print('dt: ', dt)

    prb = Problem(ns)
    prb.setDt(dt)
    # set up model
    model = FullModelInverseDynamics(problem=prb,
                                     kd=kd,
                                     q_init=q_init,
                                     base_init=base_init)

    for contact in contacts:
        model.setContactFrame(contact, 'vertex', dict(vertex_frames=[contact]))

    displacement_x = 1.7
    fin_q = model.q0.copy()
    fin_q[0] = fin_q[0] + displacement_x

    model.q.setBounds(model.q0, model.q0, 0)
    # model.q[0].setBounds(fin_q[0], fin_q[0], ns)

    init_v = np.zeros(model.nv)
    model.v.setBounds(init_v, init_v, 0)
    model.v.setBounds(init_v, init_v, ns)

    model.q.setInitialGuess(model.q0)

    for f_name, f_var in model.fmap.items():
        f_var.setInitialGuess([0, 0, kd.mass() / 4 * 9.8])

    # crawling
    gait_matrix = np.array([[1, 0, 0, 0],
                            [0, 0, 1, 0],
                            [0, 0, 0, 1],
                            [0, 1, 0, 0]]).astype(int)

    contact_pos = dict()

    # step-up
    cycle_duration = 20  # int(ns / tf * 1.5)
    print(f'cycle duration: {cycle_duration}')
    duty_cycle = 1.
    flight_with_duty = int(cycle_duration / gait_matrix.shape[1] * duty_cycle)
    n_init_nodes = 4

    step_z = 0.3
    clearance = 0.1

    pg = PatternGenerator(ns, contacts)
    stance_nodes, swing_nodes, cycle_duration = pg.generateCycle_old(gait_matrix, cycle_duration, duty_cycle=duty_cycle)

    n_cycles = int((ns - n_init_nodes) / cycle_duration) - 1
    for contact in contacts:
        i = cycle_duration
        swing_nodes_temp = swing_nodes[contact].copy()
        stance_nodes_temp = stance_nodes[contact].copy()
        for cycle in range(n_cycles):
            swing_nodes[contact].extend([i + elem for elem in swing_nodes_temp])
            stance_nodes[contact].extend([i + elem for elem in stance_nodes_temp])
            i += cycle_duration

    for key, value in stance_nodes.items():
        stance_nodes[key] = [elem + n_init_nodes for elem in value]
    for key, value in swing_nodes.items():
        swing_nodes[key] = [elem + n_init_nodes for elem in value]
    for contact in contacts:
        for x in range(n_init_nodes - 1, -1, -1):
            stance_nodes[contact].insert(0, x)

    for contact in contacts:
        [stance_nodes[contact].append(i) for i in range(swing_nodes[contact][-1] + 1, ns)]

    print('stance_nodes:')
    for name, nodes in stance_nodes.items():
        print(f'{name}:, {nodes}')
    print('swing_nodes:')
    for name, nodes in swing_nodes.items():
        print(f'{name}:, {nodes}')

    subcyle_duration = int(cycle_duration / len(contacts))

    print(f'subcyle_duration: {subcyle_duration}')

    x_trj = dict()
    x_nodes = dict()
    advancement_x = dict()
    for c in contacts:

        FK = kd.fk(c)
        contact_pos[c] = FK(q=model.q0)['ee_pos']

        # compute ending point of x for each gait cycle
        advancement_x[c] = np.linspace(0, displacement_x,
                                       len(swing_nodes[c][subcyle_duration - 1::subcyle_duration]) + 2).flatten()

        # add starting position of robot
        advancement_x[c] = contact_pos[c][0] + advancement_x[c]

        # compute for each cycle the x-pos at each node
        x_trj[c] = np.array([])
        for i_lin in range(advancement_x[c].shape[0] - 1):
            temp = np.linspace(advancement_x[c][i_lin], advancement_x[c][i_lin + 1], subcyle_duration)
            x_trj[c] = np.append(x_trj[c], temp)

        # compute swing nodes
        x_nodes[c] = swing_nodes[c][2::subcyle_duration] + swing_nodes[c][subcyle_duration - 1::subcyle_duration]
        x_nodes[c] = sorted(x_nodes[c])

    x_des = dict()
    x_pos_cnsrt = dict()
    z_des = dict()
    clea = dict()

    # contact velocity is zero, and normal force is positive
    for i, frame in enumerate(contacts):
        FK = kd.fk(frame)
        DFK = kd.frameVelocity(frame, kd_frame)

        p = FK(q=model.q)['ee_pos']
        v = DFK(q=model.q, qdot=model.v)['ee_vel_linear']

        # kinematic contact
        contact = prb.createConstraint(f"{frame}_vel", v, nodes=stance_nodes[frame])

        # unilateral forces
        fcost = horizon_utils.barrier(model.fmap[frame][2] - 10.0)  # fz > 10
        unil = prb.createIntermediateCost(f'{frame}_unil', 1e1 * fcost, nodes=stance_nodes[frame])

        # clearance
        contact_pos[frame] = FK(q=model.q0)['ee_pos']
        z_des[frame] = prb.createParameter(f'{frame}_z_des', 1)
        clea[frame] = prb.createConstraint(f"{frame}_clea", p[2] - z_des[frame], nodes=swing_nodes[frame])

        x_des[frame] = prb.createParameter(f'{frame}_x_des', 1)
        # x_pos_cnsrt[frame] = prb.createResidual(f"{frame}_x_pos", 50 * (p[0] - x_des[frame]), nodes=swing_nodes[frame])
        x_pos_cnsrt[frame] = prb.createConstraint(f"{frame}_x_pos", p[0] - x_des[frame], nodes=x_nodes[frame])

        if swing_nodes[frame]:
            model.fmap[frame].setBounds(np.array([[0, 0, 0]] * len(swing_nodes[frame])).T,
                                        np.array([[0, 0, 0]] * len(swing_nodes[frame])).T,
                                        nodes=swing_nodes[frame])

    # joint posture
    black_list_indices = list()
    white_list_indices = list()
    black_list = []
    white_list = []
    if flag_upper_body:
        black_list = ['shoulder_yaw_1', 'shoulder_pitch_1', 'elbow_pitch_1', 'shoulder_yaw_2', 'shoulder_pitch_2',
                      'elbow_pitch_2']
        white_list = ['shoulder_yaw_1', 'shoulder_pitch_1', 'elbow_pitch_1', 'shoulder_yaw_2', 'shoulder_pitch_2',
                      'elbow_pitch_2']

    postural_joints = np.array(list(range(7, model.nq)))
    for joint in black_list:
        black_list_indices.append(model.joint_names.index(joint))
    for joint in white_list:
        white_list_indices.append(7 + model.joint_names.index(joint))
    postural_joints = np.delete(postural_joints, black_list_indices)

    prb.createResidual("min_q", 0.05 * (model.q[postural_joints] - model.q0[postural_joints]))
    if white_list:
        prb.createResidual("min_q_white_list", 1. * (model.q[white_list_indices] - model.q0[white_list_indices]))

    # joint acceleration
    prb.createIntermediateResidual("min_q_ddot", 0.001 * model.a)
    # if white_list:
    #     prb.createIntermediateResidual("min_q_ddot_arms", 0.1 * model.a[white_list_indices])

    # contact forces
    for f_name, f_var in model.fmap.items():
        prb.createIntermediateResidual(f"min_{f_var.getName()}", 0.001 * f_var)

    tg = TrajectoryGenerator()

    step_x = 0.7
    for c in contacts:
        # ==============================================================================================================
        num_cycle_before_step = np.where(advancement_x[c][1:] < step_x)[0].shape[0]

        step_before = contact_pos[c][2].elements()[0]
        step_after = contact_pos[c][2].elements()[0] + step_z
        rep_param = np.array([])

        for i in range(subcyle_duration + 5):
            if i == num_cycle_before_step:
                rep_param = np.append(rep_param,
                                      tg.from_derivatives(flight_with_duty, step_before, step_after, step_z + clearance,
                                                          [0, 0, 0]))
            elif i > num_cycle_before_step:
                rep_param = np.append(rep_param,
                                      tg.from_derivatives(flight_with_duty, step_after, step_after, clearance,
                                                          [0, 0, 0]))
            else:
                rep_param = np.append(rep_param,
                                      tg.from_derivatives(flight_with_duty, step_before, step_before, clearance,
                                                          [0, 0, 0]))
        # ==============================================================================================================
        x_values = np.atleast_2d(x_trj[c])
        z_values = np.atleast_2d(rep_param)

        # x_des[c].assign(x_trj, nodes=swing_nodes[c][subcyle_duration-1::subcyle_duration])
        x_des[c].assign(x_values[:, :len(swing_nodes[c])], nodes=swing_nodes[c])
        z_des[c].assign(z_values[:, :len(swing_nodes[c])], nodes=swing_nodes[c])

        print(f'================ contact: {c} ================')
        print('z_values: \n', z_des[c].getValues()[:, clea[c].getNodes()])
        print('at nodes:', clea[c].getNodes())
        print("~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~")
        print('x_values: \n', x_des[c].getValues()[:, x_pos_cnsrt[c].getNodes()])
        print('at nodes:', x_pos_cnsrt[c].getNodes())

    model.setDynamics()

    if solver_type != 'ilqr':
        Transcriptor.make_method(transcription_method, prb, transcription_opts)

    opts = {'ipopt.max_iter': 200,
            # 'ipopt.tol': 1e-4,
            # 'ipopt.constr_viol_tol': 1e-3,
            'ilqr.max_iter': 200,
            'ilqr.alpha_min': 0.01,
            'ilqr.step_length_threshold': 1e-9,
            'ilqr.line_search_accept_ratio': 1e-4,
            }

    # todo if receding is true ....
    solver_bs = Solver.make_solver(solver_type, prb, opts)

    try:
        solver_bs.set_iteration_callback()
    except:
        pass

    t = time.time()
    solver_bs.solve()
    elapsed = time.time() - t
    print(f'bootstrap solved in {elapsed} s')

    print('launching multiprocess')
    size_box_x = 1.5
    callable_box = functools.partial(marker_spawner.make_box, pos=[step_x + size_box_x / 2, 0, step_z / 2 - 0.05],
                                     size=[size_box_x, 3, step_z])
    p1 = multiprocessing.Process(target=callable_box)
    p1.start()

    solution = solver_bs.getSolutionDict()

    # append torques to solution
    tau = list()
    id_fn = kin_dyn.InverseDynamics(kd, contacts, kd_frame)
    for i in range(solution['q'].shape[1] - 1):
        tau.append(id_fn.call(solution['q'][:, i], solution['v'][:, i], solution['a'][:, i],
                              {name: solution['f_' + name][:, i] for name in model.fmap}))

    mm2Tom2 = 1e-6
    I = np.diag([9.9994591e2 * mm2Tom2] * (model.nq - 7))
    k = 0.129
    gear_ratio = 1. / 30.
    current = list()
    for i in range(solution['q'].shape[1] - 1):
        current.append((I @ solution['a'][6:, i] / gear_ratio + tau[i][6:] * gear_ratio) / k)

    solution['tau'] = tau
    solution['current'] = current

    return prb, solution
