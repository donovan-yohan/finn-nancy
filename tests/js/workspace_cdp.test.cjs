const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');
const {inspectPage} = require('../browser/workspace_ui.cjs');
const source = fs.readFileSync(path.join(__dirname, '../browser/workspace_ui.cjs'), 'utf8');

test('workspace disclosure uses a complete printable Space event', async () => {
  const start = source.indexOf("await cdp.call('Input.dispatchKeyEvent'", source.indexOf('// One DOM, exact money'));
  const end = source.indexOf('\n      await cdp.until', start);
  assert.ok(start > 0 && end > start);
  const packets = [];
  await vm.runInNewContext(`(async()=>{${source.slice(start, end)}})()`, {
    cdp: {call: async (method, params) => packets.push(JSON.parse(JSON.stringify(params)))},
  });
  assert.deepEqual(packets, [
    {type: 'keyDown', key: ' ', code: 'Space', windowsVirtualKeyCode: 32, nativeVirtualKeyCode: 49, text: ' ', unmodifiedText: ' '},
    {type: 'keyUp', key: ' ', code: 'Space', windowsVirtualKeyCode: 32, nativeVirtualKeyCode: 49},
  ]);
});

// Narrow DOM doubles exercise observation logic only. Acceptance still renders
// generated_workspace with actual templates, foundation CSS and native Chromium.
function observe({controls=[],nodes=[],current=[],scrollers=[],pageWidth=390}={}) {
  const body={};
  const document={body,styleSheets:[],documentElement:{scrollWidth:pageWidth,dataset:{theme:'dark',bsTheme:'dark'}},
    getElementById:()=>null,
    querySelector(selector) {
      if(selector==='h1')return {textContent:'Generated'};
      if(selector==='[data-theme-select]')return {value:'dark'};
      return {};
    },
    querySelectorAll(selector) {
      if(selector==='body *')return nodes;
      if(selector.startsWith('button,'))return controls;
      if(selector.startsWith('.fn-nav > a'))return current;
      if(selector.startsWith('.fn-table-wrap'))return scrollers;
      if(selector==='main'||selector==='h1')return [{}];
      return [];
    }};
  return vm.runInNewContext(`(${inspectPage.toString()})()`,{document,innerWidth:390,
    getComputedStyle:n=>({backgroundColor:'rgb(0, 0, 0)',overflowX:n.overflowX||'visible'})});
}
function node({visible=true,width=100,height=48,right=width,parentElement=null,local=false,name='',tabIndex=-1,overflowX='visible',foundation=false}={}) {
  return {tagName:'BUTTON',name,textContent:'Generated control',className:'test',parentElement,tabIndex,overflowX,
    scrollWidth:width,clientWidth:width,checkVisibility:()=>visible,
    getBoundingClientRect:()=>({x:0,y:0,left:0,right,width,height}),
    getAttribute:key=>key==='aria-label'?name:null,
    matches:selector=>selector==='.skip-link'?false:selector.startsWith('.fn-table-wrap')?local:foundation};
}

test('closed native disclosure rectangles do not become visible touch failures', () => {
  const hidden=node({visible:false,width:10,height:10,right:900});
  const result=observe({controls:[hidden],nodes:[hidden]});
  assert.equal(result.targets.length,0);
  assert.equal(result.offenders.length,0);
});
test('primary current observation excludes hidden secondary links by selector', () => {
  const result=observe({current:[node(),node({visible:false})]});
  assert.equal(result.current,1);
});
test('only contained named keyboard-scrollable table wrappers permit local overflow', () => {
  const wrapper=node({local:true,name:'Generated evidence',tabIndex:0,overflowX:'auto',width:300});
  const content=node({width:700,right:700,parentElement:wrapper});
  assert.equal(observe({nodes:[content]}).offenders.length,0);
  wrapper.tabIndex=-1;
  assert.equal(observe({nodes:[content]}).offenders.length,1);
  wrapper.tabIndex=0;
  wrapper.getAttribute=()=>null;
  assert.equal(observe({nodes:[content]}).offenders.length,1);
  assert.equal(observe({nodes:[],pageWidth:700}).overflow,true);
});
test('foundation targets retain 48px while ordinary controls retain 44px', () => {
  const result=observe({controls:[node({height:44,foundation:true}),node({height:43})]});
  assert.equal(result.targets.length,1);
  assert.equal(result.smallFoundation.length,1);
});
