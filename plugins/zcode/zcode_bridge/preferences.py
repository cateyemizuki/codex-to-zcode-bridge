"""OpenAI structured settings: native plugin controls, no skill invocation."""
import copy
from .transport import local_call
from .storage import NOTIFY_RANGES

READ_TOOL = 'zcode_preferences_read'
UPDATE_TOOL = 'zcode_preferences_update'
CAPABILITY = {'readTool':READ_TOOL, 'updateTool':UPDATE_TOOL}
PATH_TITLES = {'zcode_executable':'ZCode 程序','engine_entry':'ZCode 引擎',
    'node_executable':'Node 程序（留空时自动查找）', 'builtin_provider_file':'内置模型配置文件',
    'personal_provider_file':'个人模型配置文件'}
NOTIFY_TITLES = {
    'completion_notify_poll_seconds':'通知轮询间隔（秒）',
    'completion_notify_start_timeout_seconds':'轮次启动等待上限（秒）',
    'completion_notify_expiry_hours':'订阅过期时间（小时）',
    'completion_notify_wake_attempts':'宿主唤醒最大尝试次数',
}
NOTIFY_DEFAULTS = {'completion_notify_poll_seconds':1,
    'completion_notify_start_timeout_seconds':120,
    'completion_notify_expiry_hours':6,'completion_notify_wake_attempts':5}


def catalogue():
    cfg = local_call('zcode_settings')
    try:
        rows = local_call('zcode_models', {'workspace':cfg['default_workspace']})['available']
    except Exception:
        # Keep native path controls available when the engine cannot connect.
        # This is the stored selection, never a fabricated live catalogue.
        rows = [{'ref':cfg['default_model'],'providerLabel':'当前配置（连接未就绪）',
            'label':cfg['default_model']['modelId'] or '未选择模型','_unavailable':True,
            'reasoning':{'levels':[{'value':cfg['default_thought_level']}],
                         'defaultLevel':cfg['default_thought_level']}}]
    choices = {}
    for row in rows:
        label = row.get('providerLabel',row['ref']['providerId'])+' · '+row.get('label',row['ref']['modelId'])
        if label in choices:
            label += ' ['+row['ref']['providerId']+']'
        choices[label] = row
    return cfg, choices


def effective_values(cfg, choices):
    values = copy.deepcopy(cfg)
    current = cfg['default_model']
    values['default_model'] = next((name for name,row in choices.items()
        if row['ref']['providerId']==current['providerId'] and row['ref']['modelId']==current['modelId']),
        current['providerId']+' / '+current['modelId'] if current['modelId'] else '未选择模型')
    return values


def read():
    cfg, choices = catalogue()
    values = effective_values(cfg, choices)
    for key in NOTIFY_TITLES:
        values.setdefault(key, cfg.get(key, NOTIFY_DEFAULTS[key]))
    options = list(choices)
    if values['default_model'] not in options:
        options.append(values['default_model'])
    levels = sorted({'auto'} | {level['value'] for row in choices.values() for level in row.get('reasoning',{}).get('levels',[])})
    if cfg['default_thought_level'] not in levels:
        levels.append(cfg['default_thought_level'])
    properties = {
        'default_workspace':{'type':'string','title':'默认工作区','minLength':1},
        'default_model':{'type':'string','title':'默认主代理模型','enum':options,
            'description':'连接未就绪时显示已保存的选择，请先检查程序路径，再重新打开设置。' if any(r.get('_unavailable') for r in choices.values()) else '从 ZCode 当前配置中读取'},
        'default_thought_level':{'type':'string','title':'默认思考强度','enum':levels,
            'description':'切换模型时自动采用它支持的默认强度；手动指定时应与该模型兼容'},
        'default_workflow_mode':{'type':'string','title':'动态工作流模式','enum':['onDemand','alwaysOn','disabled']},
        'auto_launch_desktop':{'type':'boolean','title':'ZCode 未运行时自动启动'},
        **{key:{'type':'string','title':title} for key,title in PATH_TITLES.items()},
        **{key:{'type':'integer','title':title,'minimum':NOTIFY_RANGES[key][0],
                'maximum':NOTIFY_RANGES[key][1]} for key,title in NOTIFY_TITLES.items()},
    }
    # Native settings expose schema properties only.  No global wake/auto-continue
    # authorization key exists here.
    values = {key: values[key] for key in properties if key in values}
    return {'schema':{'type':'object','properties':properties},'values':values,'layout':[
        {'kind':'group','title':'模型与工作区','items':[{'kind':'property','property':key} for key in
            ('default_workspace','default_model','default_thought_level','default_workflow_mode','auto_launch_desktop')]},
        {'kind':'group','title':'完成通知（运营参数）','items':[{'kind':'property','property':key} for key in NOTIFY_TITLES]},
        {'kind':'group','title':'配置与会话面板','items':[{'kind':'tool','tool':'zcode_open_settings'}]},
        {'kind':'group','title':'本机程序路径','items':[{'kind':'property','property':key} for key in PATH_TITLES]},
    ]}


def update(arguments):
    changes = arguments.get('set')
    if not isinstance(changes,dict) or not changes:
        raise ValueError('set 必须包含要更新的配置项')
    cfg, choices = catalogue()
    allowed = {'default_workspace','default_model','default_thought_level','default_workflow_mode','auto_launch_desktop',*PATH_TITLES,*NOTIFY_TITLES}
    if set(changes)-allowed:
        raise ValueError('未知配置项')
    changes = copy.deepcopy(changes)
    for key,value in changes.items():
        if key == 'auto_launch_desktop':
            if type(value) is not bool:
                raise ValueError('配置值类型错误：'+key)
        elif key in NOTIFY_RANGES:
            low, high = NOTIFY_RANGES[key]
            if type(value) is not int or not low <= value <= high:
                raise ValueError('通知参数超出范围：'+key)
        elif not isinstance(value,str):
            raise ValueError('配置值类型错误：'+key)
    row = None
    if 'default_model' in changes:
        row = choices.get(changes['default_model'])
        if row is None:
            raise ValueError('指定模型已不在 ZCode 配置中，请重新读取设置')
        changes['default_model'] = row['ref']
        if 'default_thought_level' not in changes:
            levels = [v['value'] for v in row.get('reasoning',{}).get('levels',[])]
            if cfg['default_thought_level'] not in levels:
                changes['default_thought_level'] = row.get('reasoning',{}).get('defaultLevel') or (levels[0] if levels else '')
    if row is None:
        current = cfg['default_model']
        row = next((r for r in choices.values() if r['ref']==current), None)
    if row and 'default_thought_level' in changes:
        valid = [v['value'] for v in row.get('reasoning',{}).get('levels',[])]
        if changes['default_thought_level'] != 'auto' and changes['default_thought_level'] not in valid:
            raise ValueError('该模型支持的思考强度：'+', '.join(valid))
    # This method is app-only and invoked by the native settings controls after
    # a person's edit. Model-driven updates still use zcode_settings + caller mode.
    saved = local_call('zcode_settings',{'changes':changes,'caller_permission_mode':'full_access'})
    return {'values':effective_values(saved, choices)}


def definitions():
    types = {'default_workspace':{'type':'string'},'default_model':{'type':'string'},
        'default_thought_level':{'type':'string'},'default_workflow_mode':{'type':'string','enum':['onDemand','alwaysOn','disabled']},
        'auto_launch_desktop':{'type':'boolean'},**{k:{'type':'string'} for k in PATH_TITLES},
        **{key:{'type':'integer','minimum':NOTIFY_RANGES[key][0],
                'maximum':NOTIFY_RANGES[key][1]} for key in NOTIFY_TITLES}}
    meta = {'ui':{'visibility':['app']},'openai/widgetAccessible':True}
    return [
        {'name':READ_TOOL,'title':'读取插件设置','description':'Read native ChatGPT/Codex plugin settings schema, values and layout.',
         'inputSchema':{'type':'object','properties':{},'additionalProperties':False},
         'outputSchema':{'type':'object','properties':{'schema':{'type':'object'},'values':{'type':'object'},'layout':{'type':'array','items':{'type':'object'}}},'required':['schema','values']},
         'annotations':{'readOnlyHint':True},'_meta':meta},
        {'name':UPDATE_TOOL,'title':'保存插件设置','description':'Apply changes made by a person in the native plugin settings UI.',
         'inputSchema':{'type':'object','properties':{'set':{'type':'object','properties':types,'minProperties':1,'additionalProperties':False}},'required':['set'],'additionalProperties':False},
         'outputSchema':{'type':'object','properties':{'values':{'type':'object'}},'required':['values']},
         'annotations':{'readOnlyHint':False,'destructiveHint':False},'_meta':meta},
    ]
