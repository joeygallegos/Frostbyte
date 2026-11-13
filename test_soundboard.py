"""Exercise browser routing and failure behavior without a broker or JS packages."""

import json
import shutil
import subprocess
import unittest

from audio_manager import PAGE


@unittest.skipUnless(shutil.which("node"), "Node.js is needed for browser-script tests")
class SoundboardTests(unittest.TestCase):
    def test_soundboard_playback_destinations_and_failures(self):
        # Small DOM/media doubles let us execute the shipped script, including
        # its startup and navigation, without producing sound or publishing MQTT.
        script = r"""
const assert=require('node:assert/strict'),vm=require('node:vm');
const page=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
class Element{
  constructor(){this.children=[];this.dataset={};this.value='';this.classes=new Set();this.style={setProperty(){}};this.classList={add:(...names)=>names.forEach(n=>this.classes.add(n)),remove:(...names)=>names.forEach(n=>this.classes.delete(n)),toggle(){}}}
  append(...items){this.children.push(...items)}
  replaceChildren(...items){this.children=items}
  setAttribute(key,value){this[key]=value}
  removeAttribute(key){delete this[key]}
  pause(){this.paused=true}
  load(){}
  async play(){this.paused=false;if(this.fail)throw Error('unplayable')}
}
const elements=new Map();
const get=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id)};
const tabs=['files','soundboard','activity','payloads'].map(name=>{const e=new Element();e.dataset.tab=name;return e});
const panels=tabs.map(tab=>{const e=new Element();e.id=tab.dataset.tab+'-panel';return e});
const requests=[];let names=['bell & chime.wav','<test>.mp3'],listFails=false,publishFails=false;
const context=vm.createContext({document:{querySelector:get,querySelectorAll:selector=>selector==='[data-tab]'?tabs:panels,createElement:()=>new Element()},location:{hash:'#soundboard'},window:{addEventListener(){}},setTimeout,Date,console,
  fetch:async(url,options)=>{requests.push({url,options});return {ok:url==='/api/queue'?!publishFails:!listFails,json:async()=>names,text:async()=>'Broker unavailable'}}});
get('#soundboard-output').value='local';
vm.runInContext(page.split('<script>')[1].split('</script>')[0],context);
const run=code=>vm.runInContext(code,context),flush=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
  await flush();
  const board=get('#soundboard'),player=get('#soundboard-player'),output=get('#soundboard-output'),status=get('#soundboard-message');
  assert.equal(board.children.length,2);
  assert.equal(board.children[1].children[0].textContent,'<test>.mp3');
  await board.children[0].onclick();
  assert.equal(player.src,'/api/files/bell%20%26%20chime.wav');
  assert.match(status.textContent,/Playing on this device/);
  assert.equal(requests.filter(r=>r.url==='/api/queue').length,0);
  player.fail=true;
  await board.children[1].onclick();
  assert.match(status.textContent,/Could not play/);
  assert.equal(board.children[1].disabled,false);
  output.value='mqtt';output.onchange();await flush();
  assert.equal(player.paused,true);assert.equal(player.hidden,true);assert.equal(player.src,undefined);
  await board.children[0].onclick();
  const publish=requests.find(r=>r.url==='/api/queue');
  assert.equal(JSON.parse(publish.options.body).clip,names[0]);
  assert.match(status.textContent,/Queued/);
  publishFails=true;
  await board.children[0].onclick();
  assert.equal(status.textContent,'Broker unavailable');assert.equal(board.children[0].disabled,false);
  output.value='local';output.onchange();await flush();assert.equal(player.hidden,false);
  // A newer tap or destination change must suppress stale playback feedback.
  player.play=()=>new Promise(resolve=>{player.finish=resolve});
  const pending=board.children[0].onclick();output.value='mqtt';output.onchange();await flush();
  player.finish();await pending;assert.doesNotMatch(status.textContent||'',/Playing on this device/);
  names=[];await run('refreshSoundboard()');assert.match(status.textContent,/No sounds yet/);
  listFails=true;await run('refreshSoundboard()');assert.match(status.textContent,/Could not load sounds/);
  run('selectTab("unknown")');assert.equal(tabs[0]['aria-selected'],true);
})().catch(error=>{console.error(error);process.exitCode=1});
"""
        result = subprocess.run(
            [shutil.which("node"), "-e", script],
            input=json.dumps(PAGE), text=True, capture_output=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
