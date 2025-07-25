#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import fixtures
from oslo_utils.fixture import uuidsentinel as uuids
from oslo_utils import timeutils

from nova.api.openstack.compute import server_diagnostics
from nova.compute import vm_states
from nova import objects
from nova.policies import base as base_policy
from nova.policies import server_diagnostics as policies
from nova.tests.unit.api.openstack import fakes
from nova.tests.unit import fake_instance
from nova.tests.unit.policies import base


class ServerDiagnosticsPolicyTest(base.BasePolicyTest):
    """Test Server Diagnostics APIs policies with all possible context.

    This class defines the set of context with different roles
    which are allowed and not allowed to pass the policy checks.
    With those set of context, it will call the API operation and
    verify the expected behaviour.
    """

    def setUp(self):
        super(ServerDiagnosticsPolicyTest, self).setUp()
        self.controller = server_diagnostics.ServerDiagnosticsController()
        self.req = fakes.HTTPRequest.blank('', version='2.48')

        self.mock_get_instance = self.useFixture(
            fixtures.MockPatch('nova.api.openstack.common.get_instance')).mock
        self.instance = fake_instance.fake_instance_obj(
                self.project_member_context, project_id=self.project_id,
                id=1, uuid=uuids.fake_id, vm_state=vm_states.ACTIVE,
                task_state=None, launched_at=timeutils.utcnow())
        self.mock_get_instance.return_value = self.instance

        self.mock_get_diagnostics = self.useFixture(
            fixtures.MockPatch('nova.compute.api.API.get_instance_diagnostics')
        ).mock
        self.diagnostics = objects.Diagnostics(
            config_drive=False,
            state='running',
            driver='libvirt',
            uptime=5,
            hypervisor='hypervisor',
            # hypervisor_os is unset
            cpu_details=[],
            nic_details=[],
            disk_details=[],
            num_cpus=4,
            num_disks=1,
            num_nics=1,
            memory_details=objects.MemoryDiagnostics(maximum=8192, used=3072),
        )
        self.mock_get_diagnostics.return_value = self.diagnostics

        # With legacy rule, any admin is able get server diagnostics.
        self.project_admin_authorized_contexts = [
            self.legacy_admin_context, self.system_admin_context,
            self.project_admin_context]

    def test_server_diagnostics_policy(self):
        rule_name = policies.BASE_POLICY_NAME
        self.common_policy_auth(self.project_admin_authorized_contexts,
                                rule_name, self.controller.index,
                                self.req, self.instance.uuid)


class ServerDiagnosticsNoLegacyNoScopeTest(ServerDiagnosticsPolicyTest):
    """Test Server Diagnostics API policies with deprecated rules
    disabled, but scope checking still disabled.
    """

    without_deprecated_rules = True


class ServerDiagnosticsScopeTypePolicyTest(ServerDiagnosticsPolicyTest):
    """Test Server Diagnostics APIs policies with system scope enabled.

    This class set the nova.conf [oslo_policy] enforce_scope to True
    so that we can switch on the scope checking on oslo policy side.
    It defines the set of context with scoped token
    which are allowed and not allowed to pass the policy checks.
    With those set of context, it will run the API operation and
    verify the expected behaviour.
    """

    def setUp(self):
        super(ServerDiagnosticsScopeTypePolicyTest, self).setUp()
        self.flags(enforce_scope=True, group="oslo_policy")
        # With scope enabled, system admin is not allowed.
        self.project_admin_authorized_contexts = [
            self.legacy_admin_context, self.project_admin_context]


class ServerDiagnosticsScopeTypeNoLegacyPolicyTest(
    ServerDiagnosticsScopeTypePolicyTest):
    """Test Server Diagnostics APIs policies with system scope enabled,
    and no more deprecated rules.
    """
    without_deprecated_rules = True


class ServerDiagnosticsOverridePolicyTest(
    ServerDiagnosticsScopeTypeNoLegacyPolicyTest):
    """Test Server Diagnostics APIs policies with system and project scoped
    but default to system roles only are allowed for project roles
    if override by operators. This test is with system scope enable
    and no more deprecated rules.
    """

    def setUp(self):
        super(ServerDiagnosticsOverridePolicyTest, self).setUp()
        rule = policies.BASE_POLICY_NAME
        # NOTE(gmann): override the rule to project member and verify it
        # work as policy is project scoped.
        self.policy.set_rules({
            rule: base_policy.PROJECT_MEMBER},
            overwrite=False)

        # Check that project member role as override above
        # is able to get server diagnostics.
        self.project_admin_authorized_contexts = [
            self.project_admin_context, self.project_manager_context,
            self.project_member_context]
