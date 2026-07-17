import Foundation

public enum ControlPage {
    public static let html = #"""
    <!doctype html>
    <html lang="zh-CN">
    <head>
      <meta charset="utf-8">
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <title>局域网截图</title>
      <style>
        :root { color-scheme: light dark; font-family: -apple-system, BlinkMacSystemFont, sans-serif; }
        body { min-height: 100vh; margin: 0; display: grid; place-items: center; background: Canvas; color: CanvasText; }
        main { width: min(28rem, calc(100% - 2rem)); text-align: center; }
        h1 { font-size: 1.75rem; margin: 0 0 2rem; letter-spacing: 0; }
        button { width: 100%; min-height: 3.25rem; border: 0; border-radius: 8px; font: inherit; font-weight: 600; color: white; background: #1769aa; cursor: pointer; }
        button:disabled { cursor: wait; opacity: .55; }
        #status { min-height: 1.5rem; margin: 1rem 0 0; font-size: .95rem; }
      </style>
    </head>
    <body>
      <main>
        <h1>局域网截图</h1>
        <button id="capture" type="button">立即截图</button>
        <p id="status" aria-live="polite">准备就绪</p>
      </main>
      <script>
        const button = document.getElementById('capture');
        const statusLine = document.getElementById('status');
        const labels = { pending: '等待截图', running: '正在截图', completed: '截图完成', failed: '截图失败', expired: '请求已过期' };
        async function poll(task) {
          try {
            const response = await fetch('/api/v1/tasks/' + task.id);
            if (!response.ok) throw new Error();
            const current = await response.json();
            statusLine.textContent = labels[current.status] || '状态未知';
            if (current.status === 'pending' || current.status === 'running') {
              setTimeout(poll, 500, current);
            } else {
              button.disabled = false;
            }
          } catch (_) {
            statusLine.textContent = '连接失败';
            button.disabled = false;
          }
        }
        button.addEventListener('click', async () => {
          button.disabled = true;
          statusLine.textContent = '正在提交';
          try {
            const response = await fetch('/api/v1/tasks', {method:'POST'});
            if (!response.ok) throw new Error();
            const task = await response.json();
            statusLine.textContent = labels[task.status] || '等待截图';
            setTimeout(poll, 500, task);
          } catch (_) {
            statusLine.textContent = '连接失败';
            button.disabled = false;
          }
        });
      </script>
    </body>
    </html>
    """#
}
