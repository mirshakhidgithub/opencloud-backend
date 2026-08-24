"""Networking endpoints (spec §3.5): what the current project has on the wire."""

from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.audit.models import AuditLog
from apps.audit.services import record
from apps.authentication import vault
from apps.common.concurrency import gather
from apps.common.exceptions import AppError
from apps.integrations.zadara import resources as zadara_resources
from apps.integrations.zadara.exceptions import ZadaraError


class NetworkOverviewView(APIView):
    """
    GET /api/v1/user/networks — VPCs with their subnets, firewalls and gateways.

    One call rather than five: these pieces are meaningless apart (a subnet
    without its VPC, a rule without its group), and the client would otherwise
    fan out and stitch them together itself. Each kind is fetched independently,
    so a refusal on one leaves the rest intact and is named in `meta.unavailable`.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        token = vault.get(request.session.session_key) if request.session.session_key else None
        if not token:
            raise AppError(message='Session expired, please sign in again.', code='session_expired', status_code=401)

        # All eight reads are independent, so they go at once rather than in
        # turn: measured 4453 ms sequentially against 1167 ms together. Machine
        # names ride along in the same wave — they are only needed to label the
        # addresses, and waiting for them afterwards added a whole round-trip.
        results = gather(
            {
                'vpcs': lambda: zadara_resources.list_vpcs(token),
                'subnets': lambda: zadara_resources.list_subnets(token),
                'security groups': lambda: zadara_resources.list_security_groups(token),
                'firewall rules': lambda: zadara_resources.list_security_group_rules(token),
                'elastic ips': lambda: zadara_resources.list_elastic_ips(token),
                'internet gateways': lambda: zadara_resources.list_internet_gateways(token),
                'nat gateways': lambda: zadara_resources.list_nat_gateways(token),
                'machines': lambda: zadara_resources.list_vms(token),
            }
        )

        # `machines` is a labelling nicety, not a section of the page, so its
        # absence is not worth reporting to the user.
        sections = [name for name in results if name != 'machines']
        unavailable = [name for name in sections if not results[name].ok]

        if len(unavailable) == len(sections):
            raise AppError(message='Failed to load networking', code='upstream_error', status_code=502)

        vpcs = results['vpcs'].value or []
        subnets = results['subnets'].value or []
        groups = results['security groups'].value or []
        rules = results['firewall rules'].value or []
        elastic_ips = results['elastic ips'].value or []
        internet_gateways = results['internet gateways'].value or []
        nat_gateways = results['nat gateways'].value or []

        # The group document has no rule ids; Neutron does. Attach them so the
        # UI can remove a specific rule instead of rewriting the whole group.
        by_group: dict[str, list] = {}
        for rule in rules:
            by_group.setdefault(rule['groupId'], []).append(rule)

        for group in groups:
            group_rules = by_group.get(group['id'], [])
            group['rules'] = group_rules
            group['ingress'] = [r['label'] for r in group_rules if r['direction'] == 'ingress'] or group['ingress']
            group['egress'] = [r['label'] for r in group_rules if r['direction'] == 'egress'] or group['egress']

        # Names, not identifiers, are what a person reads in a table.
        vpc_names = {v['id']: v['name'] for v in vpcs}
        for subnet in subnets:
            subnet['vpcName'] = vpc_names.get(subnet['vpcId'] or '', '')
        for group in groups:
            group['vpcName'] = vpc_names.get(group['vpcId'] or '', '')
        for gateway in nat_gateways:
            gateway['vpcName'] = vpc_names.get(gateway['vpcId'] or '', '')

        vm_names = {vm['id']: vm['name'] for vm in results['machines'].value or []}

        for address in elastic_ips:
            address['instanceName'] = vm_names.get(address['instanceId'] or '', '')

        attached_gateways = {vpc_id for g in internet_gateways for vpc_id in g['vpcIds']}
        for vpc in vpcs:
            vpc['subnets'] = sum(1 for s in subnets if s['vpcId'] == vpc['id'])
            vpc['securityGroups'] = sum(1 for g in groups if g['vpcId'] == vpc['id'])
            vpc['hasInternetGateway'] = vpc['id'] in attached_gateways

        return Response(
            {
                'data': {
                    'vpcs': vpcs,
                    'subnets': subnets,
                    'securityGroups': groups,
                    'elasticIps': elastic_ips,
                    'natGateways': nat_gateways,
                },
                'meta': {
                    'vpcs': len(vpcs),
                    'subnets': len(subnets),
                    'securityGroups': len(groups),
                    'elasticIps': len(elastic_ips),
                    'elasticIpsFree': sum(1 for a in elastic_ips if not a['instanceId']),
                    'unavailable': unavailable,
                },
            }
        )


def _token(request) -> str:
    token = vault.get(request.session.session_key) if request.session.session_key else None
    if not token:
        raise AppError(message='Session expired, please sign in again.', code='session_expired', status_code=401)

    return token


_WRITE_STATUS = {
    'forbidden': 403,
    'not_found': 404,
    'conflict': 409,
    'invalid_request': 400,
    'write_not_allowed': 403,
}


def _opens_the_internet(cidr: str | None) -> bool:
    return (cidr or '').strip() in ('0.0.0.0/0', '::/0')


class SecurityGroupRuleView(APIView):
    """
    POST /api/v1/user/security-groups/{id}/rules — add one firewall rule.
    DELETE /api/v1/user/security-group-rules/{id} — remove one.

    Neutron has no update for a rule, so there is no edit here either: a change
    is a delete plus a create, and saying that plainly beats an "edit" button
    that silently recreates rules behind the user's back.

    Both run with the caller's own token — the cloud judges the change by their
    rights — and both are audited, because opening a port is exactly the kind of
    action someone will need to account for later.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request, group_id: str):
        token = _token(request)
        data = request.data

        direction = (data.get('direction') or 'ingress').strip().lower()
        if direction not in ('ingress', 'egress'):
            raise AppError(message='Direction must be ingress or egress.', code='invalid_request', status_code=400)

        cidr = (data.get('cidr') or '').strip() or None
        remote_group_id = (data.get('remoteGroupId') or '').strip() or None
        protocol = (data.get('protocol') or '').strip().lower() or None

        port_from = data.get('portFrom')
        port_to = data.get('portTo')
        try:
            port_from = int(port_from) if port_from not in (None, '') else None
            port_to = int(port_to) if port_to not in (None, '') else None
        except (TypeError, ValueError):
            raise AppError(message='Ports must be numbers.', code='invalid_request', status_code=400)

        # The group must be one this session can actually see.
        try:
            visible = {g['id'] for g in zadara_resources.list_security_groups(token)}
        except ZadaraError as err:
            raise AppError(message='Failed to reach the cloud', code=err.code, status_code=502)

        if group_id not in visible:
            raise AppError(message='No such security group in this project.', code='not_found', status_code=404)

        try:
            created = zadara_resources.create_security_group_rule(
                token,
                group_id,
                direction,
                protocol=protocol,
                port_from=port_from,
                port_to=port_to,
                cidr=cidr,
                remote_group_id=remote_group_id,
                description=(data.get('description') or '').strip(),
            )
        except ZadaraError as err:
            record(
                request,
                'security_group.rule.create',
                resource_type='security_group',
                resource_id=group_id,
                outcome=AuditLog.FAILURE,
                error_code=err.code,
                detail={'direction': direction, 'protocol': protocol, 'cidr': cidr},
            )
            raise AppError(message=err.message, code=err.code, status_code=_WRITE_STATUS.get(err.code, 502))

        record(
            request,
            'security_group.rule.create',
            resource_type='security_group',
            resource_id=group_id,
            resource_name=created.get('label', ''),
            detail={
                'rule': created.get('label'),
                'direction': direction,
                # Flagged in the trail itself, so an audit read shows the risky ones.
                'openToInternet': _opens_the_internet(cidr),
            },
        )

        return Response({'data': created}, status=201)


class SecurityGroupRuleDeleteView(APIView):
    permission_classes = [IsAuthenticated]

    def delete(self, request, rule_id: str):
        token = _token(request)

        try:
            rules = {r['id']: r for r in zadara_resources.list_security_group_rules(token)}
        except ZadaraError as err:
            raise AppError(message='Failed to reach the cloud', code=err.code, status_code=502)

        rule = rules.get(rule_id)
        if not rule:
            raise AppError(message='No such rule in this project.', code='not_found', status_code=404)

        try:
            zadara_resources.delete_security_group_rule(token, rule_id)
        except ZadaraError as err:
            record(
                request,
                'security_group.rule.delete',
                resource_type='security_group',
                resource_id=rule['groupId'],
                resource_name=rule['label'],
                outcome=AuditLog.FAILURE,
                error_code=err.code,
            )
            raise AppError(message=err.message, code=err.code, status_code=_WRITE_STATUS.get(err.code, 502))

        record(
            request,
            'security_group.rule.delete',
            resource_type='security_group',
            resource_id=rule['groupId'],
            resource_name=rule['label'],
            detail={'rule': rule['label'], 'direction': rule['direction']},
        )

        return Response({'data': {'detail': 'Rule removed.'}})
