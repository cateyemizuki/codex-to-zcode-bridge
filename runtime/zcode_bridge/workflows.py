"""Compact, version-specific adapter for ZCode 0.17 workflow journals."""
import re


def actor_session_id(run_id, actor):
    # ZCode 0.17 mintActorSessionId/sanitizeIdSegment, checked against the
    # real session/resume API before any ID is reported as a verified child.
    def segment(value):
        return re.sub(r'[^A-Za-z0-9.-]', '_', value.replace('_', '__'))
    return 'sess_dwf-'+segment(run_id)+'-'+segment(f"{actor['siteId']}@{actor['ordinal']}")


def topology(run_id, events):
    actors, nodes, phases = {}, {}, []
    status = 'unknown'
    stop_reason, resumable = None, None
    for event in events:
        kind, payload = event.get('type'), event.get('payload', {})
        # A resumed run keeps its ID and journal.  These are the real native
        # lifecycle events; old run-settled entries must not survive a new start.
        if kind in ('run-started', 'run-launched'):
            status = 'running'
            stop_reason, resumable = None, None
        if kind == 'run-settled':
            status = payload.get('status', status)
            stop_reason = payload.get('stopReason')
            resumable = payload.get('resumable')
        if kind == 'phase-entered':
            phases.append({'name':payload.get('name'),'ordinal':payload.get('ordinal')})
        actor = payload.get('actor')
        if kind == 'actor-created' and actor:
            sid = actor_session_id(run_id, actor)
            actors[sid] = {'session_id':sid, 'run_id':run_id, 'actor':actor,
                'name':payload.get('name'), 'phase':payload.get('phaseName'),
                'configured_model':payload.get('persona', {}).get('model')}
        instance = payload.get('instance')
        if instance:
            key = (instance['siteId'], instance['ordinal'])
            node = nodes.setdefault(key, {'instance':instance})
            if actor:
                node['session_id'] = actor_session_id(run_id, actor)
            for source, target in (('kind','kind'),('phaseName','phase'),('instructionsHead','task'),('outcome','outcome')):
                if source in payload:
                    node[target] = payload[source]
            node['status'] = kind.removeprefix('node-')
    result = {'run_id':run_id,'status':status,'phases':phases,
              'actors':list(actors.values()),'nodes':list(nodes.values())}
    if status == 'stopped':
        if isinstance(stop_reason, str) and stop_reason:
            result['stop_reason'] = stop_reason
        if type(resumable) is bool:
            result['resumable'] = resumable
    return result
