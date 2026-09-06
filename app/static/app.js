const csrf = document.querySelector('meta[name="csrf-token"]').content;
const permissions = new Set((document.querySelector('meta[name="permissions"]')?.content || '').split(',').filter(Boolean));

function requiredPermission(url) {
  const path = new URL(url, location.origin).pathname;
  if (/\/(approve|reject|publish|verify)$/.test(path)) return 'approve';
  if (path.startsWith('/api/channels/') || path.startsWith('/api/jobs/')) return 'channel.sync';
  if (path.startsWith('/api/sync-failures/')) return 'sync_failure.manage';
  if (path.startsWith('/api/purchase-order-items/') && path.endsWith('/receive')) return 'inventory.receive';
  if (path === '/api/inventory-adjustments' || path.startsWith('/api/inventory-adjustments/')) return 'inventory.adjust';
  if (path.startsWith('/api/replenishment-plans/') || path.startsWith('/api/purchase-orders/') || path.startsWith('/api/purchase-order-cancellations/')) return 'procurement.write';
  if (path.startsWith('/api/listings/')) return 'listing.write';
  if (path.startsWith('/api/tickets/') || path.startsWith('/api/service-actions/') || path.startsWith('/api/service-action-reversals/') || path.startsWith('/api/feedback')) return 'ticket.write';
  if (path.startsWith('/api/products/') || path.startsWith('/api/import/')) return 'catalog.write';
  if (path.startsWith('/api/admin/')) return '*';
  return 'read';
}

function canUse(url) {
  const required = requiredPermission(url);
  return permissions.has('*') || permissions.has(required);
}

document.querySelectorAll('.api-form').forEach(form => { if (!canUse(form.action)) form.hidden = true; });
document.querySelectorAll('[data-action]').forEach(button => { if (!canUse(button.dataset.action)) button.hidden = true; });
document.querySelectorAll('[data-bulk-action]').forEach(button => { if (!canUse(button.dataset.bulkAction)) button.hidden = true; });

const wait = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));

async function waitForJob(jobId, notice) {
  for (let attempt = 0; attempt < 120; attempt += 1) {
    const response = await fetch(`/api/jobs/${jobId}`);
    const job = await response.json();
    if (!response.ok) throw new Error(job.detail || '无法读取同步任务');
    notice.textContent = `同步任务 #${jobId}：${job.current_step}（${job.progress}%）`;
    if (job.status === 'succeeded') return job.result || job;
    if (job.status === 'failed') throw new Error(job.last_error || '同步任务进入失败队列');
    await wait(1000);
  }
  throw new Error('同步任务仍在运行，可稍后刷新页面查看');
}

async function submitAction(url, body, trigger) {
  const notice = document.querySelector('#notice');
  notice.textContent = '处理中…';
  notice.className = 'show';
  if (trigger) trigger.disabled = true;
  try {
    const response = await fetch(url, {method: 'POST', body, headers: {'X-CSRF-Token': csrf}});
    let data = await response.json();
    if (!response.ok) throw new Error(data.detail || '操作失败');
    if (data.job_id && ['queued', 'running'].includes(data.job_status)) data = await waitForJob(data.job_id, notice);
    notice.textContent = '完成：' + JSON.stringify(data);
    notice.className = 'show ok';
    setTimeout(() => location.reload(), 600);
  } catch (error) {
    notice.textContent = error.message;
    notice.className = 'show error';
    if (trigger) trigger.disabled = false;
  }
}

document.querySelectorAll('.api-form').forEach(form => form.addEventListener('submit', event => {
  event.preventDefault();
  submitAction(form.action, new FormData(form), form.querySelector('button[type="submit"], button:not([type])'));
}));
document.querySelectorAll('[data-action]').forEach(button => button.addEventListener('click', () => {
  const body = new FormData();
  body.append('version', button.dataset.version);
  if (button.dataset.requireReason) {
    const reason = window.prompt('请输入拒绝原因（至少 3 个字）：');
    if (!reason) return;
    body.append('reason', reason);
  }
  submitAction(button.dataset.action, body, button);
}));

const selectAll = document.querySelector('#select-all-listings');
if (selectAll) selectAll.addEventListener('change', () => {
  document.querySelectorAll('.listing-select').forEach(item => { item.checked = selectAll.checked; });
});

document.querySelectorAll('[data-bulk-action]').forEach(button => button.addEventListener('click', () => {
  const values = [...document.querySelectorAll('.listing-select:checked')].map(item => item.value);
  if (!values.length) {
    window.alert('请先选择至少一条文案');
    return;
  }
  const body = new FormData();
  body.append('items', values.join(','));
  if (button.dataset.requireReason) {
    const reason = window.prompt('请输入统一拒绝原因（至少 3 个字）：');
    if (!reason) return;
    body.append('reason', reason);
  }
  submitAction(button.dataset.bulkAction, body, button);
}));

const logout = document.querySelector('#logout');
if (logout) logout.addEventListener('click', async () => {
  await fetch('/logout', {method: 'POST', headers: {'X-CSRF-Token': csrf}});
  location.href = '/login';
});
