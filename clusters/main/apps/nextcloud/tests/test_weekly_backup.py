import importlib.util
import copy
import json
import stat
import subprocess
import tempfile
from types import SimpleNamespace
from contextlib import ExitStack
import unittest
from pathlib import Path
from unittest.mock import patch, call

spec = importlib.util.spec_from_file_location('backup', Path(__file__).resolve().parents[1] / 'weekly-backup.py')
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)


def make_backup(pair, part, state='Completed', policy=b.POLICY):
    return {'metadata': {'name': f'b-{pair}-{part}'},
            'spec': {'labels': {'nextcloud-policy': policy, 'nextcloud-pair': pair, 'nextcloud-part': part}},
            'status': {'state': state, 'progress': 100, 'url': 's3://example/backup'}}


class BackupChecks(unittest.TestCase):
    def test_pvc_resolves_actual_csi_handle(self):
        volume = {'metadata': {'name': 'actual-handle'}, 'spec': {'backupTargetName': 'default'},
                  'status': {'state': 'attached', 'robustness': 'healthy'}}
        with patch.object(b, 'get', side_effect=[{'spec': {'volumeName': 'different-pv'}, 'status': {'phase': 'Bound'}},
                                               {'spec': {'csi': {'driver': 'driver.longhorn.io', 'volumeHandle': 'actual-handle'}}}, volume]) as get:
            self.assertEqual(b.volume_for_pvc('claim'), volume)
            self.assertEqual(get.call_args_list[-1], call('volumes.longhorn.io', 'actual-handle', namespace=b.LH))

    def test_refuses_split_wal(self):
        with patch.object(b, 'get', return_value={'spec': {'walStorage': {'size': '1Gi'}}}):
            with self.assertRaisesRegex(RuntimeError, 'WAL'):
                b.discover()

    def test_retains_four_complete_pairs_without_touching_unrelated_or_active(self):
        backups = [make_backup(f'2026090{day}', part) for day in range(1, 7) for part in ('files', 'database')]
        backups += [make_backup('20260810', 'files'), make_backup('20260810', 'database', 'Error')]
        backups += [make_backup('20260811', 'files', 'InProgress')]
        backups += [make_backup('20260812', 'files', policy='gameserver')]
        snapshots = [{'metadata': {'labels': {b.PAIR_LABEL: p}}} for p in ('20260810', '20260811', '20260813')]
        with patch.object(b, 'get', side_effect=[{'items': backups}, {'items': snapshots}]), patch.object(b, 'delete') as delete:
            b.prune()
        deleted_backups = {c.args[1] for c in delete.call_args_list if c.args[0] == 'backups.longhorn.io'}
        self.assertEqual(deleted_backups, {f'b-{p}-{part}' for p in ('20260901', '20260902', '20260810') for part in ('files', 'database')})
        self.assertNotIn('b-20260811-files', deleted_backups)
        self.assertNotIn(call('snapshots.longhorn.io', 'nc-20260906-files', namespace=b.LH), delete.call_args_list)
        self.assertIn(call('snapshots.longhorn.io', 'nc-20260813-files', namespace=b.LH), delete.call_args_list)

    def test_failed_pair_does_not_remove_last_good_pair(self):
        backups = [make_backup('20260901', part) for part in ('files', 'database')]
        backups += [make_backup('20260902', 'files'), make_backup('20260902', 'database', 'Error')]
        with patch.object(b, 'get', side_effect=[{'items': backups}, {'items': []}]), patch.object(b, 'delete') as delete:
            b.prune()
        delete.assert_not_called()

    def test_upload_completion_requires_both_parts(self):
        with patch.object(b, 'get', side_effect=[make_backup('pair', 'files'), make_backup('pair', 'database', 'Error')]):
            with self.assertRaisesRegex(RuntimeError, 'database backup failed'):
                b.wait_backups('pair')

    def test_completed_upload_requires_remote_url(self):
        backup = make_backup('pair', 'files')
        backup['status']['url'] = ''
        with patch.object(b, 'get', return_value=backup):
            with self.assertRaisesRegex(RuntimeError, 'completion lacks'):
                b.wait_backups('pair')

    def test_watchdog_ignores_active_quiesce(self):
        value = {'phase': 'quiescing', 'resumeAfter': 1000}
        with patch.object(b, 'state', return_value=({}, value)), patch.object(b, 'resume') as resume:
            b.recover()
            resume.assert_not_called()
        with patch.object(b, 'state', return_value=({'metadata': {}}, value)), patch.object(b, 'owner_running', return_value=True), patch.object(b.time, 'time', return_value=100), patch.object(b, 'resume') as resume, patch.object(b, 'delete') as delete:
            b.recover()
            resume.assert_not_called()
            delete.assert_not_called()

    def test_watchdog_recovers_dead_owner_before_unlock(self):
        obj, value = {'metadata': {}}, {'phase': 'quiescing', 'resumeAfter': 1000}
        order = []
        with patch.object(b, 'state', return_value=(obj, value)), patch.object(b, 'owner_running', return_value=False), patch.object(b, 'resume', side_effect=lambda *x: order.append('resume')), patch.object(b, 'delete', side_effect=lambda *x, **y: order.append('delete')):
            b.recover()
        self.assertEqual(order, ['resume', 'delete'])

    def test_watchdog_preserves_state_on_failed_recovery(self):
        with patch.object(b, 'state', return_value=({'metadata': {}}, {'phase': 'quiescing'})), patch.object(b, 'owner_running', return_value=False), patch.object(b, 'resume', side_effect=RuntimeError('unavailable')), patch.object(b, 'delete') as delete:
            with self.assertRaises(RuntimeError):
                b.recover()
            delete.assert_not_called()

    def test_terminating_pod_keeps_ownership_until_terminal(self):
        pod = {'metadata': {'uid': 'uid', 'deletionTimestamp': 'now'}, 'status': {'phase': 'Running'}}
        with patch.object(b, 'get', return_value=pod):
            self.assertTrue(b.owner_running({'ownerPod': 'pod', 'ownerUID': 'uid'}))

    def test_stop_if_external_controller_restarts_nextcloud(self):
        with patch.object(b, 'owned_state', return_value=({}, {'phase': 'quiescing', 'resumeAfter': 1000})), patch.object(b.time, 'time', return_value=100), patch.object(b, 'get', return_value={'items': [{'metadata': {'name': 'app'}}]}):
            with self.assertRaisesRegex(RuntimeError, 'restarted'):
                b.assert_quiesced()

    def test_resume_precedes_upload(self):
        order = []
        pod = {'metadata': {'name': 'app', 'uid': 'app-uid'}, 'spec': {'nodeName': 'node'}}
        deploy = {'metadata': {'uid': 'deploy-uid'}, 'spec': {'replicas': 1}}
        state = {'phase': 'quiescing', 'keeperPod': 'keeper'}
        patches = [
            patch.multiple(b, POD_NAME='backup', POD_UID='12345678-1234'),
            patch.object(b, 'recover'),
            patch.object(b, 'state', return_value=(None, None)),
            patch.object(b, 'app_pod', return_value=pod),
            patch.object(b, 'get', side_effect=[deploy, pod]),
            patch.object(b, 'occ', return_value='{"maintenance":false}'),
            patch.object(b, 'discover', return_value=({'files': {'metadata': {'name': 'file-volume'}, '_boundPV': 'file-pv'}, 'database': {}}, 'db')),
            patch.object(b, 'create'),
            patch.object(b.time, 'sleep'),
            patch.object(b, 'kubectl'),
            patch.object(b, 'until'),
            patch.object(b, 'start_keeper'),
            patch.object(b, 'set_state'),
            patch.object(b, 'create_snapshot', side_effect=lambda *x: order.append('snapshot')),
            patch.object(b, 'wait_snapshot'),
            patch.object(b, 'assert_quiesced'),
            patch.object(b, 'owned_state', return_value=({}, state)),
            patch.object(b, 'resume', side_effect=lambda *x: (order.append('resume'), state.update(phase='uploading'))),
            patch.object(b, 'create_backup', side_effect=lambda *x: order.append('upload')),
            patch.object(b, 'wait_backups'),
            patch.object(b, 'prune'),
            patch.object(b, 'delete')
        ]
        with ExitStack() as stack:
            for context in patches:
                stack.enter_context(context)
            b.run()
        self.assertEqual(order, ['snapshot', 'snapshot', 'resume', 'upload', 'upload'])

    def test_maintenance_failure_still_runs_recovery(self):
        pod = {'metadata': {'name': 'app', 'uid': 'app-uid'}, 'spec': {'nodeName': 'node'}}
        deploy = {'metadata': {'uid': 'deploy-uid'}, 'spec': {'replicas': 1}}
        with patch.multiple(b, POD_NAME='backup', POD_UID='12345678-1234'), patch.object(b, 'recover'), patch.object(b, 'state', return_value=(None, None)), patch.object(b, 'app_pod', return_value=pod), patch.object(b, 'get', side_effect=[deploy, pod]), patch.object(b, 'occ', side_effect=['{"maintenance":false}', RuntimeError('exec disconnected')]), patch.object(b, 'discover', return_value=({'files': {'metadata': {'name': 'file-volume'}, '_boundPV': 'file-pv'}, 'database': {}}, 'db')), patch.object(b, 'create'), patch.object(b, 'start_keeper'), patch.object(b, 'set_state'), patch.object(b, 'owned_state', return_value=({}, {'phase': 'quiescing'})), patch.object(b, 'resume') as resume, patch.object(b, 'delete') as delete:
            with self.assertRaisesRegex(RuntimeError, 'exec disconnected'):
                b.run()
            resume.assert_called_once()
            delete.assert_called_once_with('configmap', b.STATE_NAME)

    def test_keeper_is_bound_to_original_node_and_has_no_credentials(self):
        obj = {'metadata': {'uid': 'state-uid'}}
        value = {'keeperPod': 'keeper', 'pair': 'pair'}
        pod = b.keeper_manifest(obj, value, 'original-node')
        self.assertEqual(pod['spec']['nodeName'], 'original-node')
        self.assertFalse(pod['spec']['automountServiceAccountToken'])
        self.assertTrue(pod['spec']['containers'][0]['volumeMounts'][0]['readOnly'])
        self.assertEqual(pod['metadata']['ownerReferences'][0]['uid'], 'state-uid')

    def test_detach_requires_longhorn_and_kubernetes_attachment_release(self):
        value = {'fileVolume': 'volume', 'filePV': 'pv'}
        attachment = {'spec': {'source': {'persistentVolumeName': 'pv'}}, 'status': {'attached': True}}
        with patch.object(b, 'get', side_effect=[{'status': {'state': 'detached'}}, {'items': [attachment]}]):
            self.assertFalse(b.file_volume_detached(value))
        with patch.object(b, 'get', side_effect=[{'status': {'state': 'detached'}}, {'items': []}]):
            self.assertTrue(b.file_volume_detached(value))
        with patch.object(b, 'get', return_value={'status': {'state': 'attached'}}):
            self.assertFalse(b.file_volume_detached(value))

    def test_remove_keeper_rejects_another_pairs_pod(self):
        with patch.object(b, 'get', return_value={'metadata': {'labels': {b.PAIR_LABEL: 'other'}}}), patch.object(b, 'delete') as delete:
            with self.assertRaisesRegex(RuntimeError, 'ownership changed'):
                b.remove_keeper({'keeperPod': 'keeper', 'pair': 'pair'})
            delete.assert_not_called()

    def resume_scenario(self, stage='detach', detach_failure=False, scale_down=True):
        obj = {'metadata': {'uid': 'state-uid'}}
        value = {'phase': 'quiescing', 'deploymentUID': 'deployment-uid', 'ownerUID': 'owner',
                 'replicas': 1, 'maintenanceOwned': True, 'scaleDownAttempted': scale_down,
                 'restartStage': stage, 'keeperPod': 'keeper', 'fileVolume': 'volume', 'filePV': 'pv'}
        memory = [copy.deepcopy(value)]
        order = []
        def write_state(_obj, new):
            memory[0] = copy.deepcopy(new)
            return obj
        def read_state():
            return obj, copy.deepcopy(memory[0])
        def ready(_description, check, **_kwargs):
            result = check()
            if not result:
                raise TimeoutError(_description)
            return result
        def detached(_value):
            order.append('detach')
            return not detach_failure
        def kube(*args, **_kwargs):
            if args[0] == 'scale':
                order.append(args[-1])
        def get(resource, *_args, **_kwargs):
            if resource == 'deployment':
                return {'metadata': {'uid': 'deployment-uid'}}
            if resource == 'pods':
                return {'items': []}
            raise AssertionError(resource)
        with patch.multiple(b, POD_NAME='recover', POD_UID='recover-uid'), patch.object(b, 'set_state', side_effect=write_state), patch.object(b, 'state', side_effect=read_state), patch.object(b, 'get', side_effect=get), patch.object(b, 'remove_keeper', side_effect=lambda _x: order.append('remove-keeper')), patch.object(b, 'file_volume_detached', side_effect=detached), patch.object(b, 'until', side_effect=ready), patch.object(b, 'app_pod', return_value={'metadata': {'name': 'new-app'}}), patch.object(b, 'occ', return_value='{"maintenance":false}'), patch.object(b, 'kubectl', side_effect=kube):
            if detach_failure:
                with self.assertRaisesRegex(TimeoutError, 'did not detach'):
                    b.resume(obj, value)
            else:
                b.resume(obj, value)
        return order, memory[0]

    def test_normal_and_watchdog_resume_release_rwo_before_restart(self):
        order, value = self.resume_scenario()
        self.assertEqual(order, ['remove-keeper', '--replicas=0', 'detach', '--replicas=1'])
        self.assertEqual(value['phase'], 'uploading')
        self.assertEqual(value['restartStage'], 'start')

    def test_failed_detach_never_restarts_app(self):
        order, value = self.resume_scenario(detach_failure=True)
        self.assertEqual(order, ['remove-keeper', '--replicas=0', 'detach'])
        self.assertEqual(value['phase'], 'resuming')
        self.assertEqual(value['restartStage'], 'detach')

    def test_resume_retry_after_scale_up_does_not_stop_app_again(self):
        order, value = self.resume_scenario(stage='start')
        self.assertEqual(order, ['remove-keeper', '--replicas=1'])
        self.assertEqual(value['phase'], 'uploading')

    def test_preparation_failure_does_not_stop_running_app(self):
        order, value = self.resume_scenario(scale_down=False)
        self.assertEqual(order, ['remove-keeper', '--replicas=1'])
        self.assertEqual(value['phase'], 'uploading')

    def test_parallel_recovery_actor_cannot_stop_restarting_app(self):
        value = {'phase': 'resuming', 'recoveryOwnerUID': 'other', 'recoveryOwnerPod': 'other-pod', 'recoveryDeadline': 1000}
        actor = {'metadata': {'uid': 'other'}, 'status': {'phase': 'Running'}}
        with patch.multiple(b, POD_UID='this'), patch.object(b, 'get', return_value=actor), patch.object(b.time, 'time', return_value=100), patch.object(b, 'kubectl') as kube:
            with self.assertRaisesRegex(RuntimeError, 'already restoring'):
                b.resume({}, value)
            kube.assert_not_called()

    def test_each_kubectl_call_reads_rotated_token_without_argv_exposure(self):
        seen = []
        paths = []
        def execute(command, **kwargs):
            path = Path(command[command.index('--kubeconfig') + 1])
            config = json.loads(path.read_text())
            seen.append(config['users'][0]['user']['token'])
            paths.append(path)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(config['clusters'][0]['cluster']['server'], 'https://10.96.0.1:443')
            self.assertNotIn(seen[-1], ' '.join(command))
            self.assertNotIn(seen[-1], str(kwargs))
            return SimpleNamespace(returncode=0, stdout='{}', stderr='')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'token').write_text('old-token')
            with patch.object(b, 'SERVICE_ACCOUNT_DIR', root), patch.dict(b.os.environ, {'KUBERNETES_SERVICE_HOST': '10.96.0.1', 'KUBERNETES_SERVICE_PORT_HTTPS': '443'}, clear=True), patch.object(b.subprocess, 'run', side_effect=execute):
                b.kubectl('get', 'pods')
                (root / 'token').write_text('rotated-token')
                b.kubectl('get', 'pods')
            self.assertEqual(seen, ['old-token', 'rotated-token'])
            self.assertTrue(all(not path.exists() for path in paths))

    def test_kubectl_ipv6_api_endpoint_uses_projected_ca(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'token').write_text('credential')
            def execute(command, **_kwargs):
                config = json.loads(Path(command[command.index('--kubeconfig') + 1]).read_text())
                cluster = config['clusters'][0]['cluster']
                self.assertEqual(cluster['server'], 'https://[fd00::1]:6443')
                self.assertEqual(cluster['certificate-authority'], str(root / 'ca.crt'))
                self.assertNotIn('insecure-skip-tls-verify', cluster)
                return SimpleNamespace(returncode=0, stdout='{}', stderr='')
            with patch.object(b, 'SERVICE_ACCOUNT_DIR', root), patch.dict(b.os.environ, {'KUBERNETES_SERVICE_HOST': 'fd00::1', 'KUBERNETES_SERVICE_PORT': '6443'}, clear=True), patch.object(b.subprocess, 'run', side_effect=execute):
                b.kubectl('get', 'pods')

    def test_kubectl_errors_and_timeouts_do_not_expose_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'token').write_text('private-token')
            paths = []
            def execute(command, **_kwargs):
                paths.append(Path(command[command.index('--kubeconfig') + 1]))
                if len(paths) == 1:
                    return SimpleNamespace(returncode=1, stdout='private-token', stderr='private-token')
                raise subprocess.TimeoutExpired(command, 1, output='private-token', stderr='private-token')
            with patch.object(b, 'SERVICE_ACCOUNT_DIR', root), patch.dict(b.os.environ, {'KUBERNETES_SERVICE_HOST': '10.96.0.1'}, clear=True), patch.object(b.subprocess, 'run', side_effect=execute):
                for expected in ('failed', 'timed out'):
                    with self.assertRaisesRegex(RuntimeError, expected) as error:
                        b.kubectl('get', 'pods', timeout=1)
                    self.assertNotIn('private-token', str(error.exception))
                    self.assertTrue(all(not path.exists() for path in paths))

    def test_kubectl_refuses_implicit_localhost_fallback(self):
        with patch.dict(b.os.environ, {}, clear=True), patch.object(b.subprocess, 'run') as execute:
            with self.assertRaisesRegex(RuntimeError, 'KUBERNETES_SERVICE_HOST is missing'):
                b.kubectl('get', 'pods')
            execute.assert_not_called()

    def test_app_ready_waits_for_the_whole_pod_including_nginx(self):
        pod = {'metadata': {'name': 'app'}, 'status': {
            'containerStatuses': [{'name': 'nextcloud', 'ready': True,
                                   'state': {'running': {'startedAt': 'now'}}}]}}
        for conditions, expected in (([], False), ([{'type': 'Ready', 'status': 'False'}], False),
                                     ([{'type': 'Ready', 'status': 'True'}], True)):
            with self.subTest(conditions=conditions):
                pod['status']['conditions'] = conditions
                with patch.object(b, 'get', return_value={'items': [pod]}):
                    self.assertEqual(b.app_pod(require_ready=True), pod if expected else None)

    def test_maintenance_reset_can_exec_before_the_pod_is_ready(self):
        pod = {'metadata': {'name': 'app'}, 'status': {
            'conditions': [{'type': 'Ready', 'status': 'False'}],
            'containerStatuses': [{'name': 'nextcloud', 'ready': False,
                                   'state': {'running': {'startedAt': 'now'}}}]}}
        with patch.object(b, 'get', return_value={'items': [pod]}):
            self.assertEqual(b.app_pod(), pod)
            self.assertIsNone(b.app_pod(require_ready=True))


if __name__ == "__main__":
    unittest.main()
