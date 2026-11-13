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
    for event in events:
        kind, payload = event.get('type'), event.get('payload', {})
        if kind == 'run-settled':
            status = payload.get('status', status)
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
    return {'run_id':run_id,'status':status,'phases':phases,'actors':list(actors.values()),'nodes':list(nodes.values())}
