const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const {fragmentState,visibleFragment,moneyCents} = require('../browser/workspace_review.cjs');

// Exercise the exact browser observer with controlled painted-text rectangles.
// The real generated Chromium matrix independently exercises these refusals.
function observe({hash='#row',missing=false,covered=false,offscreen=false,hidden=false,noAmount=false,wrapped=false}={}) {
  const rect=(left,top,right,bottom)=>({left,top,right,bottom,width:right-left,height:bottom-top});
  const element=(text,rects)=>({textContent:text,rects,checkVisibility:()=>!hidden,contains(hit){return hit===this;}});
  const heading=element('Generated contributor 001',[rect(20,20,180,40)]);
  const amount=element(' -$8.00   CAD ',wrapped?[rect(20,50,100,70),rect(20,75,100,95)]:[rect(20,offscreen?-30:50,100,offscreen?-10:70)]);
  const target={getBoundingClientRect:()=>({top:16}),querySelector:selector=>selector==='h2'?heading:noAmount?null:amount};
  const document={
    getElementById:id=>!missing&&id==='row'?target:null,
    createRange:()=>({selectNodeContents(n){this.node=n;},getClientRects(){return this.node.rects;}}),
    elementFromPoint:(_x,y)=>covered?{}:y<45?heading:amount,
  };
  return vm.runInNewContext(`(${fragmentState})('#row')`,{document,location:{hash},scrollX:0,scrollY:100,innerWidth:320,innerHeight:844});
}

test('fragment observer requires the exact target and painted heading plus amount',()=>{
  const state=observe();assert.equal(state.ready,true);assert.equal(state.amount.text,'-$8.00 CAD');
  assert.equal(observe({wrapped:true}).ready,true);
  for(const change of [{hash:'#wrong'},{missing:true},{covered:true},{offscreen:true},{hidden:true},{noAmount:true}]) {
    assert.equal(observe(change).ready,false,JSON.stringify(change));
  }
});

test('native fragment completion waits for stable geometry after visible transient frames',async()=>{
  // A moving target may be visible but must not admit the next native click yet.
  const states=[0,1,2,2,2,2].map(scrollY=>({...observe(),scrollY}));
  let calls=0;
  const result=await visibleFragment({eval:async()=>states[calls++]},'#row');
  assert.equal(calls,6);assert.equal(result.scrollY,2);assert.equal(result.ready,true);
});

test('accepted contribution money stays exact beyond Number precision and refuses loose text',()=>{
  assert.equal(moneyCents('-$83.00'),-8300n);
  assert.equal(moneyCents('-$8.00 CAD'),-800n);
  assert.equal(moneyCents('-$90,071,992,547,409.93 CAD'),-9007199254740993n);
  for(const text of ['Category contribution -$8.00 CAD','-$8.0 CAD','-$8.00 USD',' $8.00 CAD '])assert.throws(()=>moneyCents(text));
});
