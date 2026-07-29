// Chrome DevTools Protocol test client v2
const WebSocket = require('ws');
const http = require('http');

function rpc(ws, method, params={}) {
  return new Promise((resolve, reject) => {
    const id = Math.floor(Math.random() * 1e6);
    const onMessage = (raw) => {
      const msg = JSON.parse(raw.toString());
      if (msg.id === id) {
        ws.off('message', onMessage);
        if (msg.error) reject(new Error(msg.error.message));
        else resolve(msg.result || msg);
      }
    };
    ws.on('message', onMessage);
    ws.send(JSON.stringify({id, method, params}));
  });
}

(async () => {
  const targets = await new Promise((resolve, reject) => {
    http.get('http://localhost:9223/json', res => {
      let d=''; res.on('data',c=>d+=c); res.on('end',()=>resolve(JSON.parse(d)));
    }).on('error', reject);
  });

  const target = targets.find(t => t.url.includes('localhost:8765') && t.url.endsWith('/'));
  console.log('Target:', target?.url);
  const ws = new WebSocket(target.webSocketDebuggerUrl);

  ws.on('open', async () => {
    const logs = [];
    ws.on('message', raw => {
      const m = JSON.parse(raw.toString());
      if (!m.method) return;
      if (m.method === 'Runtime.consoleAPICalled') {
        logs.push(`[${m.params.type}] ` + m.params.args.map(a => a.value||a.description).join(' '));
      }
      if (m.method === 'Runtime.exceptionThrown') {
        logs.push(`[EXCEPTION] ${m.params.exceptionDetails.text}: ${m.params.exceptionDetails.exception?.description || ''}`);
      }
      if (m.method === 'Log.entryAdded') {
        logs.push(`[LOG:${m.params.entry.level}] ${m.params.entry.text}`);
      }
    });

    await rpc(ws, 'Runtime.enable');
    await rpc(ws, 'Page.enable');
    await new Promise(r => setTimeout(r, 2000));

    // Check
    const r1 = await rpc(ws, 'Runtime.evaluate', {
      expression: 'typeof monitorStart + "|" + typeof ptz + "|" + document.querySelectorAll("button[onclick]").length + "|" + document.querySelector("button[onclick*=monitorStart]")?.getAttribute("onclick")'
    });
    console.log('CHECK:', r1.result?.value);

    // Click
    const r2 = await rpc(ws, 'Runtime.evaluate', {
      expression: '(() => { const b = document.querySelector("button[onclick*=monitorStart]"); if(!b) return "no btn"; b.click(); return "clicked"; })()'
    });
    console.log('CLICK:', r2.result?.value);

    await new Promise(r => setTimeout(r, 3000));
    console.log('--- console ---');
    logs.forEach(l => console.log(l));
    if (logs.length === 0) console.log('(no console output)');
    ws.close();
    process.exit(0);
  });

  ws.on('error', e => { console.error('ws err', e); process.exit(1); });
})().catch(e => { console.error(e); process.exit(1); });
