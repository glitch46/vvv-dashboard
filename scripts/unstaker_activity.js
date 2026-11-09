/* Independent of the holder charts: their backfill must not hide this panel. */
(async () => {
  const status = document.getElementById('unstakerStatus');
  const strip = document.getElementById('unstakerWallets');
  const summary = document.getElementById('unstakerSummary');
  const history = document.getElementById('unstakerHistory');
  const canvas = document.getElementById('topUnstakersChart');
  const placeholder = document.getElementById('unstakerEmpty');
  const context = document.getElementById('unstakerContext');
  const transactionHistory = document.getElementById('unstakerWalletHistory');
  const transactionDetails = document.getElementById('unstakerWalletDetails');
  const format = value => Number(value).toLocaleString('en-US', { maximumFractionDigits: 2 });
  const short = address => `${address.slice(0, 6)}…${address.slice(-4)}`;
  const addressPattern = /^0x[0-9a-f]{40}$/i;
  const kinds = [
    { key: 'sold', label: 'Verified swaps (sold)', color: '#8A3B36' },
    { key: 'transferred', label: 'Moved to another wallet', color: '#5D7A8C' },
    { key: 'other_transfer', label: 'Other transfers (unverified)', color: '#89758F' },
    { key: 'restaked', label: 'Re-staked', color: '#B8914A' },
    { key: 'held', label: 'Still holding', color: '#6F7D4E' }
  ];
  let chart;
  let selected;
  let data;

  const daysFor = wallet => Object.prototype.hasOwnProperty.call(wallet, 'history_days')
    ? wallet.history_days : Object.prototype.hasOwnProperty.call(data, 'history_days') ? data.history_days : 90;
  const periodFor = wallet => daysFor(wallet) === null ? 'all available history' : `the last ${daysFor(wallet)} days`;
  const requestedPeriod = () => data.history_scope === 'all' ? 'all available history' : `${data.history_days || 90}-day history`;

  function explorer(kind, value, label) {
    const link = document.createElement('a');
    link.href = `https://basescan.org/${kind}/${value}`;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    link.textContent = label;
    return link;
  }

  function showHistory(wallet) {
    history.replaceChildren();
    const claims = (wallet.claims || []).slice(-10).reverse();
    if (!claims.length) return;
    const table = document.createElement('table');
    const caption = table.createCaption();
    caption.textContent = 'Recent verified claims — FIFO-attributed outcomes (VVV)';
    const head = table.createTHead().insertRow();
    ['Claim (UTC)', 'Claimed', 'Verified sold', 'Transferred', 'Re-staked', 'Held'].forEach(label => {
      const th = document.createElement('th');
      th.scope = 'col';
      th.textContent = label;
      head.appendChild(th);
    });
    const body = table.createTBody();
    claims.forEach(claim => {
      const row = body.insertRow();
      const date = row.insertCell();
      const label = claim.claimed_at.replace('T', ' ').slice(0, 16);
      if (/^0x[0-9a-f]{64}$/i.test(claim.tx)) date.appendChild(explorer('tx', claim.tx, label));
      else date.textContent = label;
      [claim.claimed, claim.sold, Number(claim.transferred) + Number(claim.other_transfer), claim.restaked, claim.held]
        .forEach(amount => { row.insertCell().textContent = format(amount); });
    });
    history.appendChild(table);
  }

  function showContext(wallet) {
    context.textContent = '';
    transactionHistory.replaceChildren();
    transactionDetails.hidden = true;
    const info = wallet.history_summary;
    if (info) {
      context.textContent = `VVV history: ${format(info.transactions)} transactions` +
        `${info.first_activity_at ? ' since ' + info.first_activity_at.slice(0, 10) : ''} · ` +
        `${format(info.staked_vvv)} VVV deposited into staking · ` +
        `${format(info.other_staking_payments)} VVV in other staking payments. These are not assumed unstake claims or sales.`;
    }
    const rows = wallet.recent_activity || [];
    if (!rows.length) return;
    transactionDetails.hidden = false;
    const table = document.createElement('table');
    table.createCaption().textContent = 'Recent VVV activity — broader wallet history, separate from the claim chart';
    const head = table.createTHead().insertRow();
    ['Time (UTC)', 'Activity', 'VVV'].forEach(label => {
      const th = document.createElement('th');
      th.scope = 'col'; th.textContent = label; head.appendChild(th);
    });
    const body = table.createTBody();
    rows.slice().reverse().forEach(activity => {
      const row = body.insertRow();
      const date = row.insertCell();
      const label = activity.at.replace('T', ' ').slice(0, 16);
      if (/^0x[0-9a-f]{64}$/i.test(activity.tx)) date.appendChild(explorer('tx', activity.tx, label));
      else date.textContent = label;
      row.insertCell().textContent = activity.action;
      row.insertCell().textContent = format(activity.amount);
    });
    transactionHistory.appendChild(table);
  }

  function select(wallet) {
    if (selected === wallet.address) return;
    selected = wallet.address;
    strip.querySelectorAll('button').forEach(button => {
      const active = button.dataset.address === selected;
      button.classList.toggle('active', active);
      button.setAttribute('aria-pressed', String(active));
    });
    status.replaceChildren(explorer('address', wallet.address, wallet.address));
    const refreshed = new Date(data.as_of);
    const stale = !Number.isNaN(refreshed.getTime()) && Date.now() - refreshed.getTime() > 12 * 3600000;
    status.append(document.createTextNode(
      ` · ${format(wallet.initiated_amount)} VVV initiated in the last 7 days · Updated ${data.as_of}${stale ? ' (refresh overdue)' : ''}`));
    history.replaceChildren();
    showContext(wallet);
    if (chart) { chart.destroy(); chart = null; }
    canvas.hidden = true;
    placeholder.hidden = false;
    placeholder.textContent = ['complete', 'stale'].includes(wallet.status)
      ? `No verified unstake claims in ${periodFor(wallet)}.` : 'Wallet history is being verified.';
    if (!['complete', 'stale'].includes(wallet.status)) {
      summary.textContent = wallet.status === 'unavailable'
        ? 'History verification is temporarily unavailable. The next scheduled refresh will retry; missing data is not zero sales.'
        : `Indexing this wallet’s ${requestedPeriod()} (${wallet.progress || 0}%). Progress is saved between refreshes.`;
      return;
    }
    if (!wallet.claim_count) {
      summary.textContent = `No verified unstake claims in ${periodFor(wallet)}. Initiated/cooling-down VVV and other staking payments are not counted as held or sold claims.`;
      if (wallet.status === 'stale') summary.prepend(`Last verified history: ${wallet.history_as_of}; loading ${requestedPeriod()}. `);
      return;
    }
    const valid = Array.isArray(data.buckets) && kinds.every(kind =>
      Array.isArray(wallet.series?.[kind.key]) && wallet.series[kind.key].length === data.buckets.length &&
      wallet.series[kind.key].every(value => Number.isFinite(value) && value >= 0));
    if (!valid) {
      summary.textContent = 'Activity data is incomplete. Waiting for a verified refresh.';
      return;
    }
    const total = key => wallet.series[key].reduce((sum, value) => sum + value, 0);
    summary.textContent = `${wallet.claim_count} verified claim${wallet.claim_count === 1 ? '' : 's'} in ${periodFor(wallet)} · ` +
      `${format(total('sold'))} VVV verified sold · ${format(total('transferred') + total('other_transfer'))} transferred · ` +
      `${format(total('restaked'))} re-staked · ${format(total('held'))} still held. Recent claims have not yet reached every time bucket.`;
    if (wallet.status === 'stale') summary.prepend(`Showing last verified history as of ${wallet.history_as_of}; loading ${requestedPeriod()}. `);
    if (wallet.unreconciled_outflow > 0) {
      summary.append(document.createTextNode(' Some outflows could not be reconciled to the available balance history.'));
    }
    canvas.hidden = false;
    placeholder.hidden = true;
    chart = new Chart(canvas, {
      type: 'bar',
      data: {
        labels: data.buckets,
        datasets: kinds.map(kind => ({ label: kind.label, data: wallet.series[kind.key],
          backgroundColor: kind.color, borderWidth: 0, stack: 'outcomes' }))
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          title: { display: true, text: `${short(wallet.address)} — activity after claimed unstakes (${periodFor(wallet)})`,
            color: '#E8E4DE', font: { size: 13, weight: 400 } },
          legend: { labels: { color: '#E8E4DE', padding: 14 } },
          tooltip: { callbacks: {
            label: context => ` ${context.dataset.label}: ${format(context.parsed.y)} VVV`,
            footer: contexts => contexts[0]?.label === 'Still holding'
              ? 'Still in the original wallet as of the refresh.' : 'Elapsed time since actual claim; FIFO attribution.'
          } }
        },
        scales: {
          x: { stacked: true, title: { display: true, text: 'Time after claim / current holding', color: '#B3AD9F' },
            ticks: { color: '#B3AD9F', maxRotation: 30 }, grid: { color: 'rgba(255,255,255,0.04)' } },
          y: { stacked: true, beginAtZero: true, title: { display: true, text: 'VVV amount', color: '#B3AD9F' },
            ticks: { color: '#B3AD9F', callback: value => format(value) }, grid: { color: 'rgba(255,255,255,0.04)' } }
        }
      }
    });
    showHistory(wallet);
  }

  try {
    const response = await fetch('data/top_unstakers.json', { cache: 'no-cache' });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    data = await response.json();
    if (data.schema_version !== 2 || !data.ranking_complete) {
      status.textContent = 'The wallet ranking and historical activity are awaiting their first verified refresh.';
      return;
    }
    const wallets = (data.wallets || []).filter(wallet => addressPattern.test(wallet.address)).slice(0, 7);
    if (!wallets.length) {
      status.textContent = `No unstake initiations recorded in the rolling last seven days. Updated ${data.as_of}.`;
      return;
    }
    wallets.forEach(wallet => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'sell-date';
      button.dataset.address = wallet.address;
      button.title = `${wallet.address} — ${format(wallet.initiated_amount)} VVV initiated in 7 days`;
      button.setAttribute('aria-label', button.title);
      button.setAttribute('aria-pressed', 'false');
      button.append(document.createTextNode(short(wallet.address)));
      const amount = document.createElement('small');
      amount.textContent = `${format(wallet.initiated_amount)} VVV`;
      button.appendChild(amount);
      button.addEventListener('click', () => select(wallet));
      button.addEventListener('mouseenter', () => select(wallet));
      button.addEventListener('focus', () => select(wallet));
      strip.appendChild(button);
    });
    select(wallets[0]);
  } catch (error) {
    status.textContent = 'Wallet activity is temporarily unavailable. The next scheduled refresh will retry.';
    console.error('Largest unstakers activity:', error);
  }
})();
