import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

class Element {
  constructor(tag) { this.tagName = tag; this.children = []; this.style = {}; this.value = ''; this.selected = false; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  setAttribute(name, value) { this[name] = value; }
  get options() { return this.children.filter(e => e.tagName === 'option'); }
  get selectedOptions() { return this.options.filter(e => e.selected); }
}
async function render(request) {
  const nodes = new Map(), calls = [];
  const document = { getElementById: id => {
    if (!nodes.has(id)) nodes.set(id, new Element('div'));
    return nodes.get(id);
  }, createElement: tag => new Element(tag), querySelector: () => new Element('div') };
  let pending = [request];
  const window = { addEventListener() {}, openai: { callTool: async (name, args) => {
    calls.push({ name, args });
    if (name === 'zcode_answer') pending = [];
    return { structuredContent: name === 'zcode_pending' ? { requests: pending } : { answered: true } };
  } } };
  window.parent = window;
  const context = vm.createContext({ document, window, location: { hostname: 'test', hash: '' }, setTimeout() {} });
  const html = fs.readFileSync(new URL('../runtime/zcode_bridge/ui/settings.html', import.meta.url), 'utf8');
  const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
  vm.runInContext(script.slice(0, script.indexOf('(async()=>{if(framed')), context);
  document.getElementById('pending-session').value='session-1';
  await vm.runInContext('renderPending()', context);
  const all = node => [node, ...node.children.flatMap(all)];
  const elements = all(nodes.get('pending'));
  return { calls, elements, answer: () => JSON.parse(JSON.stringify(calls.find(c => c.name === 'zcode_answer')?.args)) };
}

test('all permission choices include the native session option in the second position', async () => {
  const ui = await render({ id: 'perm-1', options_complete: true,
    method: 'interaction/requestPermission', params: { sessionId: 'session-1',
      options: [{ optionId: 'allow_once', label: 'Allow once', kind: 'allowOnce' },
        { optionId: 'allowSession', label: 'Always allow in this session', kind: 'allow_session' },
        { optionId: 'deny', label: 'Deny', kind: 'rejectOnce' }],
      fullAccessOption: { optionId: 'fullAccess', label: 'Full access', kind: 'custom' } } });
  const options = ui.elements.find(e => e.className === 'options');
  assert.deepEqual(options.children.map(e => e.textContent), ['Allow once', 'Always allow in this session', 'Full access', 'Deny']);
  await options.children[1].onclick();
  assert.equal(ui.answer().option_id, 'allowSession');
  assert.equal(ui.answer().session_id, 'session-1');
});

test('questions nested in input are rendered and multi-select answers remain arrays', async () => {
  const ui = await render({ id: 'question-1', method: 'interaction/requestUserInput',
    params: { sessionId: 'session-1', input: { questions: [{ question: 'Choose', header: 'Header', multiSelect: true,
      options: [{ label: 'A', value: 'one,two', description: 'Description', preview: 'Preview' }, { label: 'B' }] }] } } });
  const select = ui.elements.find(e => e.tagName === 'select');
  assert.equal(select.multiple, true);
  assert.match(select.options[0].textContent, /Description/);
  assert.equal(select.options[0].title, 'Preview');
  for (const option of select.options) option.selected = true;
  await ui.elements.find(e => e.tagName === 'button' && e.textContent === '提交回答').onclick();
  assert.deepEqual(ui.answer().response.content.answers.Choose, ['one,two', 'B']);
});

test('permission refinements use the actual free-text answer channel', async () => {
  const ui = await render({ id: 'workflow-1', method: 'interaction/requestPermission',
    params: { sessionId: 'session-1', options: [], freeText: true,
      workflowRevisionOption: { optionId: 'workflowRefine', kind: 'custom', requiresFreeText: true } } });
  ui.elements.find(e => e.tagName === 'textarea').value = 'Only one worker';
  await ui.elements.find(e => e.tagName === 'button' && e.textContent === '提交修改要求').onclick();
  assert.deepEqual(ui.answer().response, { action: 'accept', optionId: 'workflowRefine', freeText: 'Only one worker' });
});

test('real native Refine choice retains its required feedback metadata without an empty-submit button', async () => {
  const ui=await render({id:'refine-native',method:'interaction/requestPermission',params:{sessionId:'session-1',
    freeText:true,options:[{optionId:'workflowRefine',kind:'custom',label:'Refine',response:{decision:'deny'}}],
    workflowRevisionOption:{optionId:'workflowRefine',kind:'custom',label:'Request changes',requiresFreeText:true}}});
  assert.equal(ui.elements.some(e=>e.tagName==='button'&&e.textContent==='Refine'),false);
  ui.elements.find(e=>e.tagName==='textarea').value='Keep the single stage';
  await ui.elements.find(e=>e.tagName==='button'&&e.textContent==='提交修改要求').onclick();
  assert.equal(ui.answer().response.optionId,'workflowRefine');
  assert.equal(ui.answer().response.freeText,'Keep the single stage');
});

test('native answer drafts and multiple custom values survive a manual submission', async () => {
  const ui = await render({ id: 'draft-1', method: 'interaction/requestUserInput', params: {
    sessionId: 'session-1', freeText: true, answerDrafts: { Choose: ['a', 'custom,one', 'custom two'] },
    questions: [{ question: 'Choose', header: 'Header', multiSelect: true,
      options: [{ label: 'A', value: 'a' }, { label: 'B', value: 'b' }] }],
  } });
  await ui.elements.find(e=>e.tagName==='button'&&e.textContent==='提交回答').onclick();
  assert.deepEqual(ui.answer().response.content.answers.Choose, ['a', 'custom,one', 'custom two']);
});

test('manual workflow setting changes use the SDK adjustment fields and real session choice', async () => {
  const ui = await render({ id: 'settings-1', method: 'interaction/requestPermission', params: {
    sessionId: 'session-1', toolName: 'CreateWorkflow',
    options: [{ optionId: 'allowSession', kind: 'allow_session', label: 'Always allow in this session' }],
    workflowRevisionOption: { optionId: 'workflowRefine', kind: 'custom', requiresFreeText: true },
    detail: { max_concurrency: 1, adjustable_settings: { subagent_model: true, concurrency_ceiling: 16 } },
  } });
  const number=ui.elements.find(e=>e.tagName==='input'&&e.type==='number');
  number.value='2';number.onchange();
  await ui.elements.find(e=>e.tagName==='button'&&e.textContent==='Always allow in this session').onclick();
  assert.deepEqual(ui.answer().response, { optionId: 'allowSession', content: { max_concurrency: 2 } });
});

test('Hook controls submit selected actual items without offering a fake cancellation', async () => {
  const ui=await render({id:'hook-1',method:'interaction/requestWorkspaceHookReview',params:{sessionId:'session-1',
    items:[{reviewItemId:'real-item',displayName:'Hook',event:'Stop',trustState:'pending_trust',
      displayCommand:'echo example',sourcePath:'hooks.json',editable:true,configuredEnabled:true}]}});
  ui.elements.find(e=>e.tagName==='input'&&e.type==='checkbox').checked=true;
  await ui.elements.find(e=>e.tagName==='button'&&e.textContent==='信任所选脚本').onclick();
  assert.deepEqual(ui.answer().response.content,{action:'trust_selected',reviewItemIds:['real-item']});
  assert.equal(ui.elements.some(e=>e.tagName==='button'&&e.textContent==='取消'),false);
});
