import numpy as np
import pytest

import batsim_py
from batsim_py import simulator
from batsim_py import protocol
from batsim_py.events import JobEvent
from batsim_py.events import SimulatorEvent
from batsim_py.events import HostEvent
from batsim_py.jobs import Job
from batsim_py.protocol import BatsimMessage
from batsim_py.protocol import JobCompletedBatsimEvent
from batsim_py.protocol import JobSubmittedBatsimEvent
from batsim_py.protocol import NotifyBatsimEvent
from batsim_py.protocol import RequestedCallBatsimEvent
from batsim_py.protocol import ResourcePowerStateChangedBatsimEvent
from batsim_py.protocol import SimulationBeginsBatsimEvent
from batsim_py.protocol import SimulationEndsBatsimEvent
from batsim_py.resources import Host, PowerStateType
from batsim_py.simulator import SimulatorHandler
from batsim_py.simulator import Reservation

from .utils import BatsimEventAPI
from .utils import BatsimPlatformAPI


class TestSimulatorHandler:
    @pytest.fixture(autouse=True)
    def setup(self, mocker):
        mocker.patch("batsim_py.simulator.which", return_value=True)
        mocker.patch("batsim_py.simulator.subprocess.Popen")
        mocker.patch.object(protocol.NetworkHandler, 'bind')
        mocker.patch.object(protocol.NetworkHandler, 'send')

        watts = [(90, 100), (120, 130)]
        props = BatsimPlatformAPI.get_resource_properties(watt_on=watts)
        resources = [
            BatsimPlatformAPI.get_resource(0, properties=props),
            BatsimPlatformAPI.get_resource(1, properties=props),
        ]

        e = BatsimEventAPI.get_simulation_begins(resources=resources)
        events = [SimulationBeginsBatsimEvent(0, e['data'])]
        msg = BatsimMessage(0, events)

        mocker.patch.object(protocol.NetworkHandler, 'recv', return_value=msg)

    def test_batsim_not_found_must_raise(self, mocker):
        mocker.patch("batsim_py.simulator.which", return_value=None)
        with pytest.raises(ImportError) as excinfo:
            SimulatorHandler()
        assert 'Batsim' in str(excinfo.value)

    def test_start_already_running_must_raise(self):
        s = SimulatorHandler()
        s.start("p", "w")
        with pytest.raises(RuntimeError) as excinfo:
            s.start("p2", "w2")
        assert "running" in str(excinfo.value)

    def test_start_verbosity_invalid_value_must_raise(self):
        s = SimulatorHandler()
        with pytest.raises(ValueError) as excinfo:
            s.start("p", "w", verbosity="l")  # type: ignore
        assert "verbosity" in str(excinfo.value)

    def test_start_with_simulation_time_less_than_zero_must_raise(self):
        s = SimulatorHandler()
        with pytest.raises(ValueError) as excinfo:
            s.start("p2", "w2", simulation_time=-1)

        assert "simulation_time" in str(excinfo.value)

    def test_start_with_simulation_time_equal_to_zero_must_raise(self):
        s = SimulatorHandler()
        with pytest.raises(ValueError) as excinfo:
            s.start("p2", "w2", simulation_time=0)

        assert "simulation_time" in str(excinfo.value)

    def test_start_with_simulation_time_must_setup_call_request(self, mocker):
        mocker.patch("batsim_py.simulator.CallMeLaterBatsimRequest")
        s = SimulatorHandler()
        s.start("p", "w", simulation_time=100)
        batsim_py.simulator.CallMeLaterBatsimRequest.assert_called_once_with(  # type: ignore
            0, 100)

    def test_start_must_dispatch_event(self):
        def foo(h: SimulatorHandler): self.__called = True
        self.__called = False

        s = SimulatorHandler()
        s.subscribe(SimulatorEvent.SIMULATION_BEGINS, foo)
        s.start("p", "w")

        assert self.__called

    def test_start_valid(self):
        s = SimulatorHandler()
        assert not s.is_running
        s.start("p", "w")
        assert s.is_running
        assert s.platform
        assert s.current_time == 0
        assert not s.jobs
        assert not s.is_submitter_finished
        protocol.NetworkHandler.bind.assert_called_once()

    def test_close_valid(self):
        s = SimulatorHandler()
        s.start("p", "w")
        s.close()
        assert not s.is_running

    def test_close_not_running_must_not_raise(self):
        s = SimulatorHandler()
        try:
            s.close()
        except:
            raise pytest.fail("Close raised an exception.")  # type: ignore

    def test_close_call_network_close(self, mocker):
        s = SimulatorHandler()
        mocker.patch("batsim_py.protocol.NetworkHandler.close")
        s.start("p", "w")
        s.close()
        protocol.NetworkHandler.close.assert_called_once()

    def test_close_dispatch_event(self, mocker):
        def foo(h: SimulatorHandler): self.__called = True
        self.__called = False

        s = SimulatorHandler()
        s.start("p", "w")
        s.subscribe(SimulatorEvent.SIMULATION_ENDS, foo)
        s.close()
        assert self.__called

    def test_proceed_time_with_simulation_time_must_force_close(self, mocker):
        s = SimulatorHandler()
        s.start("p2", "w2", simulation_time=10)

        # setup
        e = BatsimEventAPI.get_job_submitted(res=1)
        events = [
            JobSubmittedBatsimEvent(5, e['data']),
            RequestedCallBatsimEvent(10)
        ]
        msg = BatsimMessage(10, events)
        mocker.patch.object(protocol.NetworkHandler, 'recv', return_value=msg)

        s.proceed_time()
        assert not s.is_running

    def test_proceed_time_not_running_must_raise(self, mocker):
        s = SimulatorHandler()

        with pytest.raises(RuntimeError) as excinfo:
            s.proceed_time()
        assert "running" in str(excinfo.value)

    def test_proceed_time_less_than_zero_must_raise(self, mocker):
        s = SimulatorHandler()
        s.start("p", "w")

        with pytest.raises(ValueError) as excinfo:
            s.proceed_time(-1)

        assert "time" in str(excinfo.value)

    def test_proceed_time_without_time_must_go_to_next_event(self, mocker):
        s = SimulatorHandler()
        s.start("p", "w")

        e = BatsimEventAPI.get_job_submitted()
        msg = BatsimMessage(150, [JobSubmittedBatsimEvent(150, e['data'])])
        mocker.patch.object(protocol.NetworkHandler, 'recv', return_value=msg)
        mocker.patch.object(SimulatorHandler, 'set_callback')
        s.proceed_time()
        SimulatorHandler.set_callback.assert_not_called()
        assert s.current_time == 150

    def test_proceed_time_with_time_must_setup_call_request(self, mocker):
        mocker.patch("batsim_py.simulator.SimulatorHandler.set_callback")
        s = SimulatorHandler()
        s.start("p", "w")

        msg = BatsimMessage(50, [SimulationEndsBatsimEvent(50)])
        mocker.patch.object(protocol.NetworkHandler, 'recv', return_value=msg)
        s.proceed_time(50)
        simulator.SimulatorHandler.set_callback.assert_called_once()

    def test_proceed_time_withis_submitter_finished_must_not_allow_callback(self, mocker):
        s = SimulatorHandler()
        s.start("p", "w")

        e = BatsimEventAPI.get_notify_no_more_static_job_to_submit(10)
        msg = BatsimMessage(10, [NotifyBatsimEvent(10, e['data'])])
        mocker.patch.object(protocol.NetworkHandler, 'recv', return_value=msg)
        s.proceed_time()
        assert s.is_submitter_finished

        msg = BatsimMessage(50, [SimulationEndsBatsimEvent(50)])
        mocker.patch.object(protocol.NetworkHandler, 'recv', return_value=msg)
        mocker.patch.object(SimulatorHandler, 'set_callback')
        s.proceed_time(100)
        assert not s.is_running
        assert s.current_time == 50
        SimulatorHandler.set_callback.assert_not_called()

    def test_proceed_time_with_is_submitter_finished_and_queue_must_allow_callback(self, mocker):
        s = SimulatorHandler()
        s.start("p", "w")

        e = BatsimEventAPI.get_notify_no_more_static_job_to_submit(10)
        e2 = BatsimEventAPI.get_job_submitted()
        events = [
            JobSubmittedBatsimEvent(10, e2['data']),
            NotifyBatsimEvent(10, e['data']),
        ]
        msg = BatsimMessage(10, events)
        mocker.patch.object(protocol.NetworkHandler, 'recv', return_value=msg)
        s.proceed_time()
        assert s.is_submitter_finished

        msg = BatsimMessage(50, [SimulationEndsBatsimEvent(50)])
        mocker.patch.object(protocol.NetworkHandler, 'recv', return_value=msg)
        mocker.patch.object(SimulatorHandler, 'set_callback')
        s.proceed_time(50)
        SimulatorHandler.set_callback.assert_called_once()

    def test_proceed_time_with_is_submitter_finished_and_sim_time_must_allow_callback(self, mocker):
        s = SimulatorHandler()
        s.start("p", "w", simulation_time=100)

        e = BatsimEventAPI.get_notify_no_more_static_job_to_submit(10)
        msg = BatsimMessage(10, [NotifyBatsimEvent(10, e['data'])])
        mocker.patch.object(protocol.NetworkHandler, 'recv', return_value=msg)
        s.proceed_time()
        assert s.is_submitter_finished

        msg = BatsimMessage(50, [SimulationEndsBatsimEvent(50)])
        mocker.patch.object(protocol.NetworkHandler, 'recv', return_value=msg)
        mocker.patch.object(SimulatorHandler, 'set_callback')
        s.proceed_time(50)
        SimulatorHandler.set_callback.assert_called_once()

    def test_callback_not_running_must_raise(self):
        def foo(p): pass
        s = SimulatorHandler()

        with pytest.raises(RuntimeError) as excinfo:
            s.set_callback(10, foo)

        assert "running" in str(excinfo.value)

    def test_callback_invalid_time_must_raise(self, mocker):
        def foo(p): pass
        s = SimulatorHandler()

        s.start("p", "w")
        msg = BatsimMessage(50, [RequestedCallBatsimEvent(50)])
        mocker.patch.object(protocol.NetworkHandler, 'recv', return_value=msg)
        s.proceed_time(50)

        with pytest.raises(ValueError) as excinfo:
            s.set_callback(50, foo)

        assert "at" in str(excinfo.value)

    def test_callback_must_setup_call_request(self, mocker):
        def foo(p): pass
        mocker.patch("batsim_py.simulator.CallMeLaterBatsimRequest")

        s = SimulatorHandler()
        s.start("p", "w")
        s.set_callback(50, foo)
        simulator.CallMeLaterBatsimRequest.assert_called_once_with(  # type: ignore
            0, 50)

    def test_queue(self, mocker):
        s = SimulatorHandler()
        s.start("p", "w")

        e = [
            JobSubmittedBatsimEvent(
                0, BatsimEventAPI.get_job_submitted(job_id="w!0")['data']),
            JobSubmittedBatsimEvent(
                0, BatsimEventAPI.get_job_submitted(job_id="w!1")['data']),
        ]
        msg = BatsimMessage(150, e)
        mocker.patch.object(protocol.NetworkHandler, 'recv', return_value=msg)
        s.proceed_time()

        assert s.queue and len(s.queue) == 2
        s.allocate("w!1", [0])
        assert s.queue and len(s.queue) == 1

    def test_agenda_without_platform(self, mocker):
        s = SimulatorHandler()
        assert not list(s.agenda)

    def test_agenda(self, mocker):
        s = SimulatorHandler()
        s.start("p", "w")

        e = BatsimEventAPI.get_job_submitted(res=1, walltime=100)
        e = JobSubmittedBatsimEvent(0, e['data'])
        msg = BatsimMessage(0, [e])
        mocker.patch.object(protocol.NetworkHandler, 'recv', return_value=msg)
        s.proceed_time()

        s.allocate(e.job.id, [0])

        msg = BatsimMessage(10, [RequestedCallBatsimEvent(10)])
        mocker.patch.object(protocol.NetworkHandler, 'recv', return_value=msg)
        s.proceed_time()

        agenda = [
            Reservation(0, e.job.walltime - 10),
            Reservation(1, 0),
        ]

        assert s.current_time == 10
        assert list(s.agenda) == agenda

    def test_agenda_with_job_without_walltime(self, mocker):
        s = SimulatorHandler()
        s.start("p", "w")

        e = BatsimEventAPI.get_job_submitted(res=1)
        e = JobSubmittedBatsimEvent(0, e['data'])
        msg = BatsimMessage(0, [e])
        mocker.patch.object(protocol.NetworkHandler, 'recv', return_value=msg)
        s.proceed_time()

        s.allocate(e.job.id, [0])

        msg = BatsimMessage(10, [RequestedCallBatsimEvent(10)])
        mocker.patch.object(protocol.NetworkHandler, 'recv', return_value=msg)
        s.proceed_time()

        agenda = [
            Reservation(0, np.inf),
            Reservation(1, 0),
        ]

        assert s.current_time == 10
        assert list(s.agenda) == agenda

    def test_agenda_with_multiple_jobs_in_one_host(self, mocker):
        s = SimulatorHandler()
        s.start("p", "w")

        e1 = BatsimEventAPI.get_job_submitted(
            job_id="w!0", res=1, walltime=100)
        e1 = JobSubmittedBatsimEvent(0, e1['data'])
        e2 = BatsimEventAPI.get_job_submitted(
            job_id="w!1", res=1, walltime=200)
        e2 = JobSubmittedBatsimEvent(0, e2['data'])
        msg = BatsimMessage(0, [e1, e2])
        mocker.patch.object(protocol.NetworkHandler, 'recv', return_value=msg)
        s.proceed_time()

        s.allocate(e1.job.id, [0])
        s.allocate(e2.job.id, [0])

        msg = BatsimMessage(10, [RequestedCallBatsimEvent(10)])
        mocker.patch.object(protocol.NetworkHandler, 'recv', return_value=msg)
        s.proceed_time()

        agenda = [
            Reservation(0, e2.job.walltime-10),
            Reservation(1, 0),
        ]

        assert s.current_time == 10
        assert list(s.agenda) == agenda

    def test_allocate_not_running_must_raise(self):
        s = SimulatorHandler()

        with pytest.raises(RuntimeError) as excinfo:
            s.allocate("1", [1, 2])

        assert "running" in str(excinfo.value)

    def test_allocate_invalid_job_must_raise(self):
        s = SimulatorHandler()
        s.start("p", "w")

        with pytest.raises(LookupError) as excinfo:
            s.allocate("1", [0])

        assert "job" in str(excinfo.value)

    def test_allocate_invalid_host_must_raise(self, mocker):
        s = SimulatorHandler()
        s.start("p", "w")

        e = BatsimEventAPI.get_job_submitted(res=1)
        msg = BatsimMessage(150, [JobSubmittedBatsimEvent(150, e['data'])])
        mocker.patch.object(protocol.NetworkHandler, 'recv', return_value=msg)
        s.proceed_time()

        with pytest.raises(LookupError) as excinfo:
            s.allocate(e['data']['job_id'], [2])

        assert "Host" in str(excinfo.value)

    def test_allocate_must_start_job_and_host(self, mocker):
        mocker.patch("batsim_py.simulator.ExecuteJobBatsimRequest")
        s = SimulatorHandler()
        s.start("p", "w")

        e = BatsimEventAPI.get_job_submitted(res=1)
        msg = BatsimMessage(150, [JobSubmittedBatsimEvent(150, e['data'])])
        mocker.patch.object(protocol.NetworkHandler, 'recv', return_value=msg)
        s.proceed_time()

        assert s.queue

        job = s.jobs[0]
        s.allocate(job.id, [0])

        assert job.is_running
        assert s.platform.get_by_id(0).is_computing
        simulator.ExecuteJobBatsimRequest.assert_called_once_with(  # type: ignore
            150, job.id, job.allocation)

    def test_allocate_start_must_dispatch_events(self, mocker):
        def foo_j(j: Job):
            self.__j_called, self.__j_id = True, j.id

        def foo_h(h: Host):
            self.__h_called, self.__h_id = True, h.id

        self.__j_called = self.__h_called = False
        self.__j_id = self.__h_id = -1
        s = SimulatorHandler()
        s.start("p", "w")
        s.subscribe(JobEvent.STARTED, foo_j)
        s.subscribe(HostEvent.STATE_CHANGED, foo_h)

        e = BatsimEventAPI.get_job_submitted(res=1)
        msg = BatsimMessage(150, [JobSubmittedBatsimEvent(150, e['data'])])
        mocker.patch.object(protocol.NetworkHandler, 'recv', return_value=msg)
        s.proceed_time()

        assert s.queue

        job = s.jobs[0]
        s.allocate(job.id, [0])
        assert self.__j_called and self.__j_id == job.id
        assert self.__h_called and self.__h_id == 0

    def test_allocate_must_init_host(self, mocker):
        mocker.patch("batsim_py.simulator.SetResourceStateBatsimRequest")
        mocker.patch("batsim_py.simulator.ExecuteJobBatsimRequest")
        s = SimulatorHandler()
        s.start("p", "w")

        # setup
        host = s.platform.get_by_id(0)
        host._switch_off()
        host._set_off()

        e = BatsimEventAPI.get_job_submitted(res=2)
        msg = BatsimMessage(150, [JobSubmittedBatsimEvent(150, e['data'])])
        mocker.patch.object(protocol.NetworkHandler, 'recv', return_value=msg)
        s.proceed_time()
        s.allocate(e['data']['job_id'], [0, 1])

        assert s.jobs[0].is_runnable
        assert host.is_switching_on
        simulator.ExecuteJobBatsimRequest.assert_not_called()  # type: ignore
        simulator.SetResourceStateBatsimRequest.assert_called_once_with(  # type: ignore
            150, [0], host.get_default_pstate().id)

    def test_allocate_must_dispatch_job_event(self, mocker):
        def foo(j: Job):
            self.__called = True
            self.__job_id = j.id
        self.__called, self.__job_id = False, -1

        s = SimulatorHandler()
        s.start("p", "w")

        e = BatsimEventAPI.get_job_submitted(res=1)
        msg = BatsimMessage(150, [JobSubmittedBatsimEvent(150, e['data'])])
        mocker.patch.object(protocol.NetworkHandler, 'recv', return_value=msg)
        s.proceed_time()
        s.subscribe(JobEvent.ALLOCATED, foo)
        job = s.jobs[0]
        s.allocate(job.id, [0])

        assert self.__called and self.__job_id == job.id

    def test_allocate_with_switching_off_host_must_not_start_job(self, mocker):
        mocker.patch("batsim_py.protocol.ExecuteJobBatsimRequest")
        s = SimulatorHandler()
        s.start("p", "w")

        # setup
        host = s.platform.get_by_id(0)
        host._switch_off()
        e = BatsimEventAPI.get_job_submitted(res=1)
        msg = BatsimMessage(150, [JobSubmittedBatsimEvent(150, e['data'])])
        mocker.patch.object(protocol.NetworkHandler, 'recv', return_value=msg)
        s.proceed_time()
        s.allocate(e['data']['job_id'], [0])

        assert s.jobs[0].is_runnable
        assert host.is_switching_off
        protocol.ExecuteJobBatsimRequest.assert_not_called()  # type: ignore

    def test_allocate_with_switching_on_host_must_not_start_job(self, mocker):
        mocker.patch("batsim_py.protocol.ExecuteJobBatsimRequest")
        s = SimulatorHandler()
        s.start("p", "w")

        # setup
        host = s.platform.get_by_id(0)
        host._switch_off()
        host._set_off()
        host._switch_on()

        e = BatsimEventAPI.get_job_submitted(res=1)
        msg = BatsimMessage(150, [JobSubmittedBatsimEvent(150, e['data'])])
        mocker.patch.object(protocol.NetworkHandler, 'recv', return_value=msg)
        s.proceed_time()
        s.allocate(e['data']['job_id'], [0])

        assert s.jobs[0].is_runnable
        assert host.is_switching_on
        protocol.ExecuteJobBatsimRequest.assert_not_called()  # type: ignore

    def test_kill_job_not_running_must_raise(self):
        s = SimulatorHandler()

        with pytest.raises(RuntimeError) as excinfo:
            s.kill_job("1")

        assert "running" in str(excinfo.value)

    def test_kill_job_not_found_must_raise(self):
        s = SimulatorHandler()
        s.start("p", "w")

        with pytest.raises(LookupError) as excinfo:
            s.kill_job("1")

        assert "job" in str(excinfo.value)

    def test_kill_job_must_sync_with_batsim_only(self, mocker):
        mocker.patch("batsim_py.simulator.KillJobBatsimRequest")
        s = SimulatorHandler()
        s.start("p", "w")

        e = BatsimEventAPI.get_job_submitted(res=1)
        msg = BatsimMessage(150, [JobSubmittedBatsimEvent(150, e['data'])])
        mocker.patch.object(protocol.NetworkHandler, 'recv', return_value=msg)
        mocker.patch.object(batsim_py.jobs.Job, '_terminate')
        mocker.patch.object(batsim_py.resources.Host, '_release')
        s.proceed_time()
        job_id = s.jobs[0].id
        s.kill_job(job_id)

        assert s.jobs
        batsim_py.jobs.Job._terminate.assert_not_called()
        batsim_py.resources.Host._release.assert_not_called()
        simulator.KillJobBatsimRequest.assert_called_once_with(  # type: ignore
            150, job_id)

    def test_reject_job_not_running_must_raise(self, mocker):
        s = SimulatorHandler()

        with pytest.raises(RuntimeError) as excinfo:
            s.reject_job("1")

        assert "running" in str(excinfo.value)

    def test_reject_job_not_found_must_raise(self, mocker):
        s = SimulatorHandler()
        s.start("p", "w")

        with pytest.raises(LookupError) as excinfo:
            s.reject_job("1")

        assert "job" in str(excinfo.value)

    def test_reject_job(self, mocker):
        mocker.patch("batsim_py.simulator.RejectJobBatsimRequest")
        s = SimulatorHandler()
        s.start("p", "w")

        e = BatsimEventAPI.get_job_submitted(res=1)
        msg = BatsimMessage(150, [JobSubmittedBatsimEvent(150, e['data'])])
        mocker.patch.object(protocol.NetworkHandler, 'recv', return_value=msg)
        mocker.patch.object(batsim_py.jobs.Job, '_reject')
        s.proceed_time()
        job_id = e['data']['job_id']
        s.reject_job(job_id)

        assert not s.jobs
        batsim_py.jobs.Job._reject.assert_called_once()
        simulator.RejectJobBatsimRequest.assert_called_once_with(  # type: ignore
            150, job_id)

    def test_reject_job_must_dispatch_event(self, mocker):
        def foo(j: Job):
            self.__called, self.__job_id = True, j.id

        self.__called, self.__job_id = False, -1

        s = SimulatorHandler()
        s.start("p", "w")
        s.subscribe(JobEvent.REJECTED, foo)

        e = BatsimEventAPI.get_job_submitted(res=1)
        msg = BatsimMessage(150, [JobSubmittedBatsimEvent(150, e['data'])])
        mocker.patch.object(protocol.NetworkHandler, 'recv', return_value=msg)
        s.proceed_time()
        job_id = e['data']['job_id']
        s.reject_job(job_id)

        assert self.__called and self.__job_id == job_id

    def test_switch_on_not_running_must_raise(self):
        s = SimulatorHandler()
        with pytest.raises(RuntimeError) as excinfo:
            s.switch_on([0])
        assert 'running' in str(excinfo.value)

    def test_switch_on_not_found_must_raise(self):
        s = SimulatorHandler()
        s.start("p", "w")
        with pytest.raises(LookupError) as excinfo:
            s.switch_on([30])
        assert 'Host' in str(excinfo.value)

    def test_switch_on(self, mocker):
        mocker.patch("batsim_py.simulator.SetResourceStateBatsimRequest")
        mocker.patch.object(batsim_py.resources.Host, '_switch_on')
        s = SimulatorHandler()
        s.start("p", "w")
        s.switch_on([1])
        ps = s.platform.get_by_id(1).get_default_pstate()
        batsim_py.resources.Host._switch_on.assert_called_once()
        simulator.SetResourceStateBatsimRequest.assert_called_once_with(  # type: ignore
            0, [1], ps.id)

    def test_switch_on_must_dispatch_host_event(self, mocker):
        def foo(h: Host):
            self.__nb_called += 1

        self.__nb_called = 0
        mocker.patch.object(batsim_py.resources.Host, '_switch_on')
        s = SimulatorHandler()
        s.start("p", "w")
        s.subscribe(HostEvent.STATE_CHANGED, foo)
        s.switch_on([0, 1])
        assert self.__nb_called == 2

    def test_switch_off_not_running_must_raise(self):
        s = SimulatorHandler()
        with pytest.raises(RuntimeError) as excinfo:
            s.switch_off([0])
        assert 'running' in str(excinfo.value)

    def test_switch_off_not_found_must_raise(self):
        s = SimulatorHandler()
        s.start("p", "w")
        with pytest.raises(LookupError) as excinfo:
            s.switch_off([10])
        assert 'Host' in str(excinfo.value)

    def test_switch_off(self, mocker):
        mocker.patch("batsim_py.simulator.SetResourceStateBatsimRequest")
        mocker.patch.object(batsim_py.resources.Host, '_switch_off')
        s = SimulatorHandler()
        s.start("p", "w")
        s.switch_off([0])
        ps = s.platform.get_by_id(0).get_sleep_pstate()
        batsim_py.resources.Host._switch_off.assert_called_once()
        simulator.SetResourceStateBatsimRequest.assert_called_once_with(  # type: ignore
            0, [0], ps.id)

    def test_switch_off_must_dispatch_host_event(self, mocker):
        def foo(h: Host):
            self.__nb_called += 1

        self.__nb_called = 0
        mocker.patch.object(batsim_py.resources.Host, '_switch_off')
        s = SimulatorHandler()
        s.start("p", "w")
        s.subscribe(HostEvent.STATE_CHANGED, foo)
        s.switch_off([0, 1])
        assert self.__nb_called == 2

    def test_switch_ps_not_running_must_raise(self):
        s = SimulatorHandler()
        with pytest.raises(RuntimeError) as excinfo:
            s.switch_power_state(0, 0)
        assert 'running' in str(excinfo.value)

    def test_switch_ps_not_found_must_raise(self):
        s = SimulatorHandler()
        s.start("p", "w")
        with pytest.raises(LookupError) as excinfo:
            s.switch_power_state(10, 0)
        assert 'Host' in str(excinfo.value)

    def test_switch_ps(self, mocker):
        mocker.patch("batsim_py.simulator.SetResourceStateBatsimRequest")
        mocker.patch.object(batsim_py.resources.Host,
                            '_set_computation_pstate')
        s = SimulatorHandler()
        s.start("p", "w")
        h = s.platform.get_by_id(0)
        ps = h.get_pstate_by_type(PowerStateType.COMPUTATION)
        assert len(ps) == 2
        s.switch_power_state(0, ps[-1].id)
        batsim_py.resources.Host._set_computation_pstate.assert_called_once()
        simulator.SetResourceStateBatsimRequest.assert_called_once_with(  # type: ignore
            0, [0], ps[-1].id)

    def test_switch_ps_must_dispatch_host_event(self, mocker):
        def foo(h: Host):
            self.__called, self.__h_id = True, h.id

        self.__called, self.__h_id = False, -1

        s = SimulatorHandler()
        s.start("p", "w")
        s.subscribe(HostEvent.COMPUTATION_POWER_STATE_CHANGED, foo)
        h = s.platform.get_by_id(0)
        ps = h.get_pstate_by_type(PowerStateType.COMPUTATION)
        s.switch_power_state(0, ps[-1].id)
        assert self.__called and self.__h_id == 0

    def test_on_batsim_job_submitted_must_append_in_queue(self, mocker):
        s = SimulatorHandler()
        s.start("p", "w")

        # Setup Allocate
        e = BatsimEventAPI.get_job_submitted(res=1)
        job_id, job_alloc = e['data']['job_id'], [0]
        msg = BatsimMessage(150, [JobSubmittedBatsimEvent(150, e['data'])])
        mocker.patch.object(protocol.NetworkHandler, 'recv', return_value=msg)
        s.proceed_time()

        assert s.queue and s.queue[0].id == job_id

    def test_on_batsim_job_submitted_must_dispatch_event(self, mocker):
        def foo(j: Job):
            self.__called, self.__j_id = True, j.id

        self.__called, self.__j_id = False, -1
        s = SimulatorHandler()
        s.start("p", "w")
        s.subscribe(JobEvent.SUBMITTED, foo)

        # Setup Allocate
        e = BatsimEventAPI.get_job_submitted(res=1)
        job_id = e['data']['job_id']
        msg = BatsimMessage(150, [JobSubmittedBatsimEvent(150, e['data'])])
        mocker.patch.object(protocol.NetworkHandler, 'recv', return_value=msg)
        s.proceed_time()

        assert self.__called and self.__j_id == job_id

    def test_on_batsim_job_completed_must_terminate_job_and_release_host(self, mocker):
        s = SimulatorHandler()
        s.start("p", "w")

        # Setup Allocate
        e = BatsimEventAPI.get_job_submitted(res=1)
        job_id, job_alloc = e['data']['job_id'], [0]
        msg = BatsimMessage(150, [JobSubmittedBatsimEvent(150, e['data'])])
        mocker.patch.object(protocol.NetworkHandler, 'recv', return_value=msg)
        s.proceed_time()
        s.allocate(job_id, job_alloc)

        # Setup Completed
        mocker.patch.object(batsim_py.jobs.Job, '_terminate')
        mocker.patch.object(batsim_py.resources.Host, '_release')
        e = BatsimEventAPI.get_job_completted(100, job_id, alloc=job_alloc)
        msg = BatsimMessage(150, [JobCompletedBatsimEvent(150, e['data'])])
        mocker.patch.object(protocol.NetworkHandler, 'recv', return_value=msg)
        s.proceed_time()

        batsim_py.jobs.Job._terminate.assert_called_once()
        batsim_py.resources.Host._release.assert_called_once()
        assert not s.jobs

    def test_on_batsim_job_completed_must_dispatch_event(self, mocker):
        def foo_j(j: Job):
            self.__j_called, self.__j_id = True, j.id

        def foo_h(h: Host):
            self.__h_called, self.__h_id = True, h.id

        self.__j_called = self.__h_called = False
        self.__j_id = self.__h_id = -1

        s = SimulatorHandler()
        s.start("p", "w")
        s.subscribe(HostEvent.STATE_CHANGED, foo_h)
        s.subscribe(JobEvent.COMPLETED, foo_j)

        # Setup Allocate
        e = BatsimEventAPI.get_job_submitted(res=1)
        job_id, job_alloc = e['data']['job_id'], [0]
        msg = BatsimMessage(150, [JobSubmittedBatsimEvent(150, e['data'])])
        mocker.patch.object(protocol.NetworkHandler, 'recv', return_value=msg)
        s.proceed_time()
        s.allocate(job_id, job_alloc)

        # Setup Completed
        mocker.patch.object(batsim_py.jobs.Job, '_terminate')
        e = BatsimEventAPI.get_job_completted(100, job_id, alloc=job_alloc)
        msg = BatsimMessage(150, [JobCompletedBatsimEvent(150, e['data'])])
        mocker.patch.object(protocol.NetworkHandler, 'recv', return_value=msg)
        s.proceed_time()

        assert self.__j_called and self.__j_id == job_id
        assert self.__h_called and self.__h_id == job_alloc[0]

    def test_on_batsim_host_ps_changed_must_set_off_and_dispatch_event(self, mocker):
        def foo_h(h: Host):
            self.__h_called, self.__h_id = True, h.id
        self.__j_id = self.__h_id = -1
        s = SimulatorHandler()
        s.start("p", "w")

        s.switch_off([0])
        assert s.platform.get_by_id(0).is_switching_off

        # Setup
        p_id = s.platform.get_by_id(0).get_sleep_pstate().id
        e = BatsimEventAPI.get_resource_state_changed(150, [0], p_id)
        e = ResourcePowerStateChangedBatsimEvent(150, e['data'])
        msg = BatsimMessage(150, [e])
        mocker.patch.object(protocol.NetworkHandler, 'recv', return_value=msg)
        s.subscribe(HostEvent.STATE_CHANGED, foo_h)
        s.proceed_time()

        assert s.platform.get_by_id(0).is_sleeping
        assert self.__h_called and self.__h_id == 0

    def test_on_batsim_host_ps_changed_must_set_on_and_dispatch_event(self, mocker):
        def foo_h(h: Host):
            self.__h_called, self.__h_id = True, h.id
        self.__j_id = self.__h_id = -1
        s = SimulatorHandler()
        s.start("p", "w")

        s.platform.get_by_id(0)._switch_off()
        s.platform.get_by_id(0)._set_off()
        s.switch_on([0])
        assert s.platform.get_by_id(0).is_switching_on

        # Setup
        p_id = s.platform.get_by_id(0).get_default_pstate().id
        e = BatsimEventAPI.get_resource_state_changed(150, [0], p_id)
        e = ResourcePowerStateChangedBatsimEvent(150, e['data'])
        msg = BatsimMessage(150, [e])
        mocker.patch.object(protocol.NetworkHandler, 'recv', return_value=msg)
        s.subscribe(HostEvent.STATE_CHANGED, foo_h)
        s.proceed_time()

        assert s.platform.get_by_id(0).is_idle
        assert self.__h_called and self.__h_id == 0

    def test_on_batsim_host_ps_changed_must_set_comp_ps_and_dispatch_event(self, mocker):
        def foo_h(h: Host):
            self.__h_called, self.__h_id = True, h.id
        self.__j_id = self.__h_id = -1
        s = SimulatorHandler()
        s.start("p", "w")

        # Setup
        host = s.platform.get_by_id(0)
        new_ps = host.get_pstate_by_type(PowerStateType.COMPUTATION)[-1]
        assert host.pstate != new_ps

        e = BatsimEventAPI.get_resource_state_changed(
            150, [host.id], new_ps.id)
        e = ResourcePowerStateChangedBatsimEvent(150, e['data'])
        msg = BatsimMessage(150, [e])
        mocker.patch.object(protocol.NetworkHandler, 'recv', return_value=msg)
        s.subscribe(HostEvent.COMPUTATION_POWER_STATE_CHANGED, foo_h)
        s.proceed_time()
        assert host.pstate == new_ps
        assert self.__h_called and self.__h_id == 0

    def test_on_batsim_simulation_ends_must_ack(self, mocker):
        s = SimulatorHandler()
        s.start("p", "w")
        msg = BatsimMessage(100, [SimulationEndsBatsimEvent(100)])
        mocker.patch.object(protocol.NetworkHandler, 'recv', return_value=msg)
        s.proceed_time()

        assert not s.is_running
        assert protocol.NetworkHandler.send.call_count == 2
