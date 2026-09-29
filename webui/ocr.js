/* ============================================================================
   文档文字识别 —— 前端逻辑
   ----------------------------------------------------------------------------
   依赖 ./app.js 提供的 API / U / UI / Jobs / Bars / Shell 与 ./icons.js 的 Icons。
   所有请求走 API.get/post（它自动处理平台前缀与鉴权头）。

   五个视图：识别 / 结果 / 检索 / 任务 / 设置。
   ========================================================================== */

(function () {
  'use strict';

  const State = {
    summary: null,
    engines: null,
    roots: [],
    extract: { inputs: [], output: '', format: 'txt', ocr: false },
    results: { documents: [], total: 0, query: '', status: 'all', offset: 0 },
    search: { query: '', mode: 'text', results: [], total: 0, hits: 0 },
    current: null,
  };

  const STATUS_KIND = {
    ok: 'ok', partial: 'warn', 'no-text-layer': 'danger', empty: 'neutral',
    encrypted: 'danger', error: 'danger', metadata: 'info', ocr: 'ok',
  };

  /* ------------------------------------------------------------ 概览统计条 */

  function statTiles(summary) {
    return U.el('div', { class: 'grid cols-4 mb2' }, [
      tile(T('已入库文档'), U.num(summary.total), U.size(summary.bytes)),
      tile(T('有文字层'), U.num(summary.with_text),
           summary.total ? U.pct(summary.with_text / summary.total * 100) : '—'),
      tile(T('无文字层（扫描页）'), U.num(summary.scanned),
           T('这些才需要真 OCR')),
      tile(T('累计字符'), U.num(summary.chars), U.num(summary.pages) + T(' 页')),
    ]);
  }

  function tile(label, value, hint) {
    return U.el('div', { class: 'stat-tile' }, [
      U.el('div', { class: 'label', text: label }),
      U.el('div', { class: 'value' }, [
        U.el('span', { text: value }),
        hint ? U.el('small', { text: hint }) : null,
      ]),
    ]);
  }

  /* ------------------------------------------------------------ 逐页字符数 */

  function pageBars(pages, onJump) {
    const host = U.el('div', { class: 'pagebars' });
    (pages || []).forEach((page) => {
      const has = page.has_text && (page.chars || 0) > 0;
      const node = U.el('span', {
        class: 'pg ' + (has ? 'has' : 'none'),
        title: `${T('第')} ${page.index} ${T('页：')}${page.chars || 0} ${T('字符')}` +
               (has ? '' : T('（无文字层）')),
      });
      if (onJump) node.addEventListener('click', () => onJump(page.index));
      host.appendChild(node);
    });
    return host;
  }

  /* ------------------------------------------------------------ 识别 */

  function renderExtract(host) {
    host.innerHTML = '';
    const card = U.el('div', { class: 'card' });
    card.appendChild(U.el('h2', {}, [
      U.el('span', { html: Icons.svg('fileText', { size: 17 }) }),
      U.el('span', { text: T('识别文档') }),
    ]));
    card.appendChild(U.el('div', { class: 'card-hint', text:
      T('选择 PDF 或图片（可整个目录），提取文字层与元数据。源文件全程只读，不会被修改。') }));

    // ---- 输入 ----
    const inputPath = U.el('div', { class: 'path empty', text: T('尚未选择') });
    const chips = U.el('div', { class: 'chips mt1' });

    const pickDirBtn = U.el('button', { class: 'btn' }, [
      U.el('span', { html: Icons.svg('folder', { size: 15 }) }), U.el('span', { text: T('添加目录') }),
    ]);
    pickDirBtn.addEventListener('click', () => {
      UI.pickDir({
        title: T('选择要处理的目录'), start: State.extract.inputs[0] || '',
        onPick: (path) => addInput(path),
      });
    });
    const pickFileBtn = U.el('button', { class: 'btn' }, [
      U.el('span', { html: Icons.svg('fileText', { size: 15 }) }),
      U.el('span', { text: T('添加 PDF / 图片') }),
    ]);
    pickFileBtn.addEventListener('click', () => {
      UI.pickDir({
        title: T('选择文档'), pickFile: true, start: State.extract.inputs[0] || '',
        onPick: (path) => addInput(path),
      });
    });

    card.appendChild(U.el('label', { class: 'field' }, [
      U.el('span', { class: 'label-text', text: T('输入（目录或文件，可多个）') }),
      U.el('div', { class: 'picker-row' }, [inputPath, pickDirBtn, pickFileBtn]),
      chips,
    ]));

    function renderChips() {
      chips.innerHTML = '';
      State.extract.inputs.forEach((path) => {
        const remove = U.el('button', { title: T('移除'), text: '×' });
        remove.addEventListener('click', () => {
          State.extract.inputs = State.extract.inputs.filter((item) => item !== path);
          refreshInputs();
        });
        chips.appendChild(U.el('span', { class: 'chip' }, [
          U.el('span', { text: path, title: path }), remove,
        ]));
      });
    }

    function refreshInputs() {
      if (State.extract.inputs.length) {
        inputPath.textContent = State.extract.inputs.length + T(' 项');
        inputPath.classList.remove('empty');
      } else {
        inputPath.textContent = T('尚未选择');
        inputPath.classList.add('empty');
      }
      renderChips();
      submit.disabled = !State.extract.inputs.length;
    }

    function addInput(path) {
      if (!path) return;
      if (State.extract.inputs.includes(path)) { UI.warn(T('已经添加过了')); return; }
      State.extract.inputs.push(path);
      refreshInputs();
    }

    // ---- 输出 ----
    const outputPath = U.el('div', {
      class: 'path' + (State.extract.output ? '' : ' empty'),
      text: State.extract.output || T('尚未选择（默认放到应用数据目录的 output/）'),
    });
    const pickOut = U.el('button', { class: 'btn' }, [
      U.el('span', { html: Icons.svg('folderOpen', { size: 15 }) }),
      U.el('span', { text: T('选择输出目录') }),
    ]);
    pickOut.addEventListener('click', () => {
      UI.pickDir({
        title: T('选择输出目录'), start: State.extract.output || '',
        onPick: (path) => {
          State.extract.output = path;
          outputPath.textContent = path;
          outputPath.classList.remove('empty');
        },
      });
    });
    card.appendChild(U.el('label', { class: 'field' }, [
      U.el('span', { class: 'label-text', text: T('输出目录') }),
      U.el('div', { class: 'picker-row' }, [outputPath, pickOut]),
      U.el('span', { class: 'help', text:
        T('重名不覆盖，自动追加 -1、-2；目录输入会按原目录结构镜像输出。') }),
    ]));

    // ---- 输出格式 ----
    const formatSelect = U.el('select', {});
    [['txt', T('TXT（纯正文，便于检索）')], ['md', T('Markdown（每页一节 + 字符数）')],
     ['json', T('JSON（逐页字符数 + 元数据）')], ['all', T('三种都输出')],
     ['none', T('不写文件（只建索引）')]].forEach(([value, label]) => {
      formatSelect.appendChild(U.el('option', { value, text: label }));
    });
    formatSelect.value = State.extract.format;
    formatSelect.addEventListener('change', () => {
      State.extract.format = formatSelect.value;
      refreshSubmit();
    });

    // ---- OCR 开关（可选引擎）----
    const ocrToggle = U.el('input', { type: 'checkbox', id: 'ocr-toggle' });
    ocrToggle.checked = State.extract.ocr;
    ocrToggle.addEventListener('change', () => { State.extract.ocr = ocrToggle.checked; });
    const ocr = (State.engines && State.engines.engines) || {};
    const ocrAvailable = !!((ocr.tesseract && ocr.tesseract.available) ||
                            (ocr.remote && ocr.remote.available));

    card.appendChild(U.el('div', { class: 'row' }, [
      U.el('label', { class: 'field' }, [
        U.el('span', { class: 'label-text', text: T('输出格式') }), formatSelect,
      ]),
      U.el('label', { class: 'field' }, [
        U.el('span', { class: 'label-text', text: T('图片 OCR（可选）') }),
        U.el('div', { class: 'checks' }, [
          U.el('label', {}, [
            ocrToggle,
            U.el('span', { text: ocrAvailable ? T('对图片调用可用的 OCR 引擎')
                                              : T('（当前没有可用引擎）') }),
          ]),
        ]),
      ]),
    ]));

    // ---- 提交 ----
    const submit = U.el('button', { class: 'btn primary', disabled: true }, [
      U.el('span', { html: Icons.svg('play', { size: 15 }) }),
      U.el('span', { text: T('开始识别') }),
    ]);
    submit.addEventListener('click', async () => {
      const params = {
        roots: [], inputs: [], output_dir: State.extract.output || '',
        format: State.extract.format, ocr: State.extract.ocr,
      };
      State.extract.inputs.forEach((path) => {
        if (path.endsWith('/') || path.endsWith('\\') ||
            !/\.[A-Za-z0-9]{2,5}$/.test(path)) params.roots.push(path);
        else params.inputs.push(path);
      });
      if (!params.roots.length && !params.inputs.length) { UI.warn(T('请先选择输入')); return; }
      submit.disabled = true;
      try {
        const job = await Jobs.submit('extract', params,
          T('识别文档（') + State.extract.inputs.length + T(' 项）'));
        if (job && job.job) State.current = job.job.id;
        Shell.show('jobs');
      } catch (error) {
        UI.err(error);
      } finally {
        refreshSubmit();
      }
    });
    card.appendChild(U.el('div', { class: 'btn-row mt1' }, [submit]));

    function refreshSubmit() {
      submit.disabled = !State.extract.inputs.length;
    }

    host.appendChild(card);
    refreshInputs();

    // ---- 说明 ----
    const tips = U.el('div', { class: 'card' }, [
      U.el('h2', {}, [
        U.el('span', { html: Icons.svg('info', { size: 17 }) }),
        U.el('span', { text: T('输出说明与能力边界') }),
      ]),
      U.el('div', { class: 'small muted prewrap', text:
        T('· TXT 只含正文，便于直接 grep\n') +
        T('· Markdown 把每页包成一节（## 第 N 页），节首标出该页字符数\n') +
        T('· JSON 含逐页字符数与元数据 —— 哪几页没有文字层一眼可见\n') +
        T('· 扫描件（整页是图片）会被明确标成「无文字层」，而不是给出空文件\n') +
        T('· 加密 PDF 会明确报「已加密，无法提取」，不会尝试破解\n') +
        T('· 源文件全程只读；输出目录不指定时落在本应用的 data/output/') }),
    ]);
    host.appendChild(tips);

    const engineCard = U.el('div', { class: 'card' }, [
      U.el('h2', {}, [
        U.el('span', { html: Icons.svg('cpu', { size: 17 }) }),
        U.el('span', { text: T('引擎') }),
      ]),
    ]);
    const t = ocr.tesseract || {};
    const r = ocr.remote || {};
    engineCard.appendChild(UI.banner('ok', T('PDF 文字层提取始终可用（完全离线）'),
      T('由应用自研的解析器完成：交叉引用 / 内容流 / FlateDecode / ToUnicode 字符映射，') +
      T('不需要任何外部程序，也不联网。')));
    engineCard.appendChild(UI.banner(
      t.available ? 'info' : 'warn',
      t.available ? T('本机 tesseract 可用') : T('本机没有 tesseract（图片 OCR 不可用）'),
      U.esc(t.detail || '') + T('<br>这不影响 PDF 与图片元数据功能。')));
    engineCard.appendChild(UI.banner(
      r.available ? 'warn' : 'info',
      r.available ? T('远程 OCR 接口已启用') : T('远程 OCR 接口未启用'),
      U.esc(r.detail || '') + (r.available
        ? T('<br><b>启用后图片会被发送到该地址</b>，请确认你信任该服务。') : '')));
    host.appendChild(engineCard);
  }

  /* ------------------------------------------------------------ 结果 */

  async function renderResults(host) {
    host.innerHTML = '';
    host.appendChild(UI.banner('info', T('正在加载…'), ''));

    let summary = null;
    try {
      summary = await API.get('api/ocr/summary');
      State.summary = summary;
    } catch (error) {
      host.innerHTML = '';
      host.appendChild(UI.banner('error', T('无法读取统计信息'), U.esc(error.message) +
        T('<ul><li>服务可能正在重启，稍后重试</li>') +
        T('<li>或查看日志：journalctl -u shh14-ocr</li></ul>')));
      return;
    }

    host.innerHTML = '';
    if (!State.roots.length) {
      const action = U.el('button', { class: 'btn primary', text: T('去设置可访问目录') });
      action.addEventListener('click', () => Shell.show('settings'));
      const banner = UI.banner('warn', T('还没有配置可访问目录'),
        T('本应用默认只读、白名单为空 —— 必须由你指定它才能读哪些目录。'));
      banner.querySelector('.bd').appendChild(U.el('div', { class: 'mt1' }, [action]));
      host.appendChild(banner);
    }

    host.appendChild(statTiles(summary));
    host.appendChild(statusBreakdown(summary));

    // 文档列表
    const card = U.el('div', { class: 'card flush' });
    const search = U.el('input', {
      type: 'search', placeholder: T('按文件名 / 路径过滤…'), value: State.results.query,
    });
    search.addEventListener('input', U.debounce(() => {
      State.results.query = search.value.trim();
      loadDocuments(bodyCard);
    }, 300));

    const statusSelect = U.el('select', {});
    [['all', T('全部状态')]].concat(Object.entries(State.engines.statuses || {})
      .map(([value, text]) => [value, text])).forEach(([value, label]) => {
      statusSelect.appendChild(U.el('option', { value, text: label }));
    });
    statusSelect.value = State.results.status;
    statusSelect.addEventListener('change', () => {
      State.results.status = statusSelect.value;
      loadDocuments(bodyCard);
    });

    card.appendChild(U.el('div', { class: 'card-head spread' }, [
      U.el('h2', { class: 'mb0' }, [
        U.el('span', { html: Icons.svg('list', { size: 17 }) }),
        U.el('span', { text: T('提取结果') }),
      ]),
      U.el('div', { class: 'btn-row' }, [search, statusSelect]),
    ]));
    const bodyCard = U.el('div', { class: 'card-body' });
    card.appendChild(bodyCard);
    host.appendChild(card);

    await loadDocuments(bodyCard, statusSelect, search);
  }

  function statusBreakdown(summary) {
    const rows = summary.statuses || [];
    if (!rows.length) return U.el('div', {});
    const card = U.el('div', { class: 'card' }, [
      U.el('h2', {}, [
        U.el('span', { html: Icons.svg('chart', { size: 17 }) }),
        U.el('span', { text: T('按状态分布') }),
      ]),
    ]);
    const host = U.el('div', {});
    card.appendChild(host);
    host.textContent = '';
    Bars.render(host, rows.map((row, index) => ({
      label: (State.engines.statuses || {})[row.status] || row.status || T('未知'),
      value: row.n,
      text: U.num(row.n) + T(' 份 · ') + U.num(row.chars) + T(' 字符'),
      color: U.color(index, 44),
    })));
    return card;
  }

  async function loadDocuments(body, statusSelect, searchInput) {
    body.innerHTML = '';
    body.appendChild(U.el('div', { class: 'empty' }, [U.el('div', { class: 'ed', text: T('加载中…') })]));
    const query = 'api/ocr/documents?limit=100'
      + '&status=' + encodeURIComponent(State.results.status)
      + '&q=' + encodeURIComponent(State.results.query);
    let data;
    try {
      data = await API.get(query);
    } catch (error) {
      body.innerHTML = '';
      body.appendChild(UI.banner('error', T('无法读取结果列表'), U.esc(error.message)));
      return;
    }
    State.results.documents = data.documents || [];
    State.results.total = data.total || 0;

    body.innerHTML = '';
    if (!State.results.documents.length) {
      body.appendChild(UI.empty('fileText', T('还没有提取结果'),
        T('到「识别」页选一个目录或 PDF，提取完这里就会有内容。')));
      return;
    }

    const table = U.el('table', { class: 'data' }, [
      U.el('thead', {}, [U.el('tr', {}, [
        U.el('th', { text: T('文档') }),
        U.el('th', { text: T('状态') }),
        U.el('th', { class: 'num', text: T('页') }),
        U.el('th', { class: 'num', text: T('字符') }),
        U.el('th', { text: T('逐页字符数') }),
        U.el('th', { text: '' }),
      ])]),
    ]);
    const tbody = U.el('tbody', {});
    State.results.documents.forEach((doc) => {
      const detail = U.el('button', { class: 'btn sm ghost', title: T('查看详情') },
        [U.el('span', { html: Icons.svg('eye', { size: 13 }) })]);
      detail.addEventListener('click', () => showDocument(doc));
      tbody.appendChild(U.el('tr', {}, [
        U.el('td', { class: 'path-cell' }, [
          U.el('div', { text: baseName(doc.path) }),
          U.el('div', { class: 'small faint mono', text: dirName(doc.path) }),
        ]),
        U.el('td', {}, [UI.badge(doc.status_label || doc.status,
                                 STATUS_KIND[doc.status] || 'neutral')]),
        U.el('td', { class: 'num', text: U.num(doc.page_count) }),
        U.el('td', { class: 'num', text: U.num(doc.chars) }),
        U.el('td', {}, [pageBars(doc.pages)]),
        U.el('td', {}, [detail]),
      ]));
      if (doc.error) {
        tbody.appendChild(U.el('tr', {}, [
          U.el('td', { colspan: '6', class: 'small', style: 'color:var(--danger)' },
            [U.el('span', { text: doc.error })]),
        ]));
      }
    });
    table.appendChild(tbody);
    body.appendChild(table);
    body.appendChild(U.el('div', { class: 'small faint mt1',
      text: T('共 ') + U.num(State.results.total) + T(' 份文档，显示前 ')
            + State.results.documents.length + T(' 份') }));
  }

  async function showDocument(doc) {
    let data = null;
    try {
      data = await API.get('api/ocr/document?path=' + encodeURIComponent(doc.path));
    } catch (error) {
      UI.err(error, T('无法读取提取结果'));
      return;
    }
    const pages = data.pages || [];
    const rows = pages.map((page) => `
      <tr>
        <td>${U.esc(String(page.index))}</td>
        <td class="num">${U.num(page.chars)}</td>
        <td>${page.has_text ? '有文字层' : '无文字层'}</td>
        <td>${page.image_count ? U.num(page.image_count) + ' 张图' : '—'}</td>
      </tr>`).join('');
    const preview = data.preview || '';
    const html = `
      <div class="kv mb2">
        <div class="k">${T('路径')}</div><div class="v">${U.esc(data.path || '')}</div>
        <div class="k">${T('状态')}</div><div class="v">${U.esc(data.status_label || data.status || '')}</div>
        <div class="k">${T('页数')}</div><div class="v">${U.num(data.page_count)}</div>
        <div class="k">${T('字符数')}</div><div class="v">${U.num(data.chars)}</div>
        ${data.error ? `<div class="k">错误</div><div class="v">${U.esc(data.error)}</div>` : ''}
      </div>
      ${pages.length ? `<table class="data">
        <thead><tr><th>页</th><th class="num">字符</th><th>文字层</th><th>图像</th></tr></thead>
        <tbody>${rows}</tbody></table>` : ''}
      ${preview ? `<h3 class="mt2">正文预览</h3>
        <div class="textview">${U.esc(preview)}</div>` : ''}
      <div class="btn-row mt2">
        <button class="btn" data-dl="txt">${T('下载 TXT')}</button>
        <button class="btn" data-dl="md">${T('下载 Markdown')}</button>
        <button class="btn" data-dl="json">${T('下载 JSON')}</button>
      </div>
      <p class="small muted mt1">${T('下载会从源文件重新提取，保证是完整文本（索引里的正文有长度上限）。')}</p>`;

    const modal = UI.modal({
      title: T('提取结果'), icon: 'fileText', wide: true, bodyHtml: html,
      buttons: [{ text: T('关闭') }],
    });
    ((modal && modal.node) || document).querySelectorAll('[data-dl]').forEach((button) => {
      button.addEventListener('click', () => {
        const fmt = button.dataset.dl;
        window.location.href = API.url('api/ocr/download?path='
          + encodeURIComponent(doc.path) + '&format=' + fmt);
      });
    });
  }

  /* ------------------------------------------------------------ 检索 */

  async function renderSearch(host) {
    host.innerHTML = '';
    const card = U.el('div', { class: 'card' });
    card.appendChild(U.el('h2', {}, [
      U.el('span', { html: Icons.svg('search', { size: 17 }) }),
      U.el('span', { text: T('全文检索') }),
    ]));
    card.appendChild(U.el('div', { class: 'card-hint', text:
      T('在已提取的文本里找关键词（数据库 LIKE 匹配，不依赖 FTS5 扩展）。') }));

    const input = U.el('input', { type: 'search', placeholder: T('要查找的关键词…'),
                                  value: State.search.query });
    const modeSelect = U.el('select', {});
    [['text', T('搜正文')], ['path', T('搜文件名 / 路径')]].forEach(([value, label]) => {
      modeSelect.appendChild(U.el('option', { value, text: label }));
    });
    modeSelect.value = State.search.mode;
    const button = U.el('button', { class: 'btn primary' }, [
      U.el('span', { html: Icons.svg('search', { size: 15 }) }),
      U.el('span', { text: T('检索') }),
    ]);

    const run = async () => {
      const query = input.value.trim();
      State.search.query = query;
      State.search.mode = modeSelect.value;
      if (!query) { UI.warn(T('请输入关键词')); return; }
      resultsHost.innerHTML = '';
      resultsHost.appendChild(U.el('div', { class: 'empty' },
        [U.el('div', { class: 'ed', text: T('检索中…') })]));
      let data;
      try {
        data = await API.get('api/ocr/search?limit=100&q=' + encodeURIComponent(query)
          + '&mode=' + modeSelect.value);
      } catch (error) {
        resultsHost.innerHTML = '';
        resultsHost.appendChild(UI.banner('error', T('检索失败'), U.esc(error.message)));
        return;
      }
      State.search.results = data.results || [];
      State.search.total = data.total || 0;
      renderSearchResults(resultsHost, query);
    };

    button.addEventListener('click', run);
    input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') run();
    });

    card.appendChild(U.el('div', { class: 'row' }, [
      U.el('label', { class: 'field' }, [
        U.el('span', { class: 'label-text', text: T('关键词') }), input,
      ]),
      U.el('label', { class: 'field' }, [
        U.el('span', { class: 'label-text', text: T('范围') }), modeSelect,
      ]),
      U.el('div', { class: 'field' }, [
        U.el('span', { class: 'label-text', text: ' ' }), button,
      ]),
    ]));
    host.appendChild(card);

    const resultsHost = U.el('div', {});
    host.appendChild(resultsHost);
    if (State.search.query) {
      renderSearchResults(resultsHost, State.search.query);
    } else {
      resultsHost.appendChild(U.el('div', { class: 'card' }, [
        U.el('div', { class: 'small muted', text:
          T('索引里还没有检索记录 —— 先在「识别」页跑一次批量提取（输出格式选「不写文件」也可以，') +
          T('同样会建立索引）。') }),
      ]));
    }
  }

  function renderSearchResults(host, query) {
    host.innerHTML = '';
    if (!State.search.results.length) {
      host.appendChild(UI.empty('search', T('没有匹配「') + query + T('」的文档'),
        T('换个关键词，或先用「识别」页把目标目录提取一遍。')));
      return;
    }
    const card = U.el('div', { class: 'card flush' });
    card.appendChild(U.el('div', { class: 'card-head' }, [
      U.el('h2', { class: 'mb0' }, [
        U.el('span', { html: Icons.svg('fileText', { size: 17 }) }),
        U.el('span', { text: T('命中 ') + U.num(State.search.total) + T(' 份文档') }),
      ]),
    ]));
    const body = U.el('div', { class: 'card-body' });
    State.search.results.forEach((row) => {
      const item = U.el('div', { class: 'mb2' }, [
        U.el('div', {}, [
          U.el('b', { text: baseName(row.path) }),
          U.el('span', { class: 'small faint mono', text: '  ' + dirName(row.path) }),
        ]),
        U.el('div', { class: 'small muted', text:
          (row.status_label || '') + ' · ' + U.num(row.page_count) + T(' 页 · ')
          + U.num(row.chars) + T(' 字符')
          + (row.hits ? T(' · 命中 ') + U.num(row.hits) + T(' 次') : '') }),
      ]);
      if (row.snippet) {
        item.appendChild(U.el('div', { class: 'textview mt1 small',
          html: highlight(row.snippet, query) }));
      }
      const actions = U.el('div', { class: 'btn-row mt1' }, []);
      const open = U.el('button', { class: 'btn sm', text: T('查看') });
      open.addEventListener('click', () => showDocument(row));
      actions.appendChild(open);
      ['txt', 'md'].forEach((fmt) => {
        const dl = U.el('button', { class: 'btn sm ghost', text: fmt.toUpperCase() });
        dl.addEventListener('click', () => {
          window.location.href = API.url('api/ocr/download?path='
            + encodeURIComponent(row.path) + '&format=' + fmt);
        });
        actions.appendChild(dl);
      });
      item.appendChild(actions);
      body.appendChild(item);
    });
    card.appendChild(body);
    host.appendChild(card);
  }

  function highlight(text, query) {
    const escaped = U.esc(text);
    if (!query) return escaped;
    const needle = U.esc(query).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    try {
      return escaped.replace(new RegExp(needle, 'gi'), (hit) => '<mark>' + hit + '</mark>');
    } catch (error) {
      return escaped;
    }
  }

  /* ------------------------------------------------------------ 任务 */

  async function renderJobs(host) {
    host.innerHTML = '';
    const card = U.el('div', { class: 'card flush' });
    card.appendChild(U.el('div', { class: 'card-head spread' }, [
      U.el('h2', { class: 'mb0' }, [
        U.el('span', { html: Icons.svg('activity', { size: 17 }) }),
        U.el('span', { text: T('任务') }),
      ]),
      U.el('div', { class: 'btn-row' }, [
        U.el('span', { class: 'small muted', id: 'job-counts' }),
      ]),
    ]));
    const body = U.el('div', { class: 'card-body' });
    card.appendChild(body);
    host.appendChild(card);

    let data;
    try {
      data = await API.get('api/jobs?limit=100');
    } catch (error) {
      body.appendChild(UI.banner('error', T('无法读取任务列表'), U.esc(error.message)));
      return;
    }
    const counts = data.counts || {};
    const node = U.byId('job-counts');
    if (node) {
      node.textContent = `${T('运行中')} ${counts.running || 0} ${T('· 排队')} ${counts.queued || 0}`
        + ` ${T('· 已完成')} ${counts.completed || 0} ${T('· 失败')} ${counts.failed || 0}`;
    }
    Jobs.renderTable(body, data.jobs || [], { emptyHint: T('还没有任务') });
  }

  /* ------------------------------------------------------------ 设置 */

  async function renderSettings(host) {
    host.innerHTML = '';
    // 界面语言（放最前：非中文用户进来第一眼就该看到它）
    // UI.langSelect() 内部已处理「落 localStorage + 套用 + 同步到后端 settings.ui_language」。
    %(host)s.appendChild(U.el('div', { class: 'card' }, [
      U.el('h2', {}, [
        U.el('span', { html: Icons.svg('globe', { size: 17 }) }),
        U.el('span', { text: T('界面语言') }),
      ]),
      U.el('div', { class: 'card-hint',
        text: T('选择本应用界面的语言。首次打开时会跟随浏览器语言。') }),
      UI.langSelect(),
    ]));

    // ---- 可访问目录 ----
    const rootsCard = U.el('div', { class: 'card' });
    rootsCard.appendChild(U.el('h2', {}, [
      U.el('span', { html: Icons.svg('shield', { size: 17 }) }),
      U.el('span', { text: T('可访问目录（白名单）') }),
    ]));
    rootsCard.appendChild(U.el('div', { class: 'card-hint', text:
      T('本应用默认只读，且白名单初始为空。只有你在这里添加的目录，应用才能读取。') }));

    const rootList = U.el('div', { class: 'chips mb1' });
    function renderRoots() {
      rootList.innerHTML = '';
      if (!State.roots.length) {
        rootList.appendChild(U.el('span', { class: 'small faint',
          text: T('（当前为空，应用读不到任何目录）') }));
        return;
      }
      State.roots.forEach((root) => {
        const remove = U.el('button', { title: T('移除'), text: '×' });
        remove.addEventListener('click', async () => {
          const confirmed = await UI.confirm({
            title: T('移除可访问目录'),
            body: T('移除后应用将无法再读取：\n') + root + T('\n\n（不会删除任何文件）'),
            confirmText: T('移除'), danger: true,
          });
          if (!confirmed) return;
          State.roots = State.roots.filter((item) => item !== root);
          await saveRoots();
          renderRoots();
        });
        rootList.appendChild(U.el('span', { class: 'chip' }, [
          U.el('span', { text: root, title: root }), remove,
        ]));
      });
    }
    renderRoots();

    const addBtn = U.el('button', { class: 'btn primary' }, [
      U.el('span', { html: Icons.svg('plus', { size: 15 }) }),
      U.el('span', { text: T('添加目录') }),
    ]);
    addBtn.addEventListener('click', () => {
      UI.pickDir({
        title: T('选择允许本应用访问的目录'), start: State.roots[0] || '',
        onPick: async (path) => {
          if (State.roots.includes(path)) { UI.warn(T('已在列表中')); return; }
          State.roots.push(path);
          await saveRoots();
          renderRoots();
        },
      });
    });
    rootsCard.appendChild(U.el('div', { class: 'btn-row' }, [addBtn]));
    rootsCard.appendChild(U.el('div', { class: 'small muted mt1', text:
      T('路径校验：先 realpath 规范化再比对白名单根。目录穿越（../）与指向白名单之外的') +
      T('软链接都会被拒绝。') }));
    host.appendChild(rootsCard);

    async function saveRoots() {
      try {
        await API.post('api/settings', { allowed_roots: State.roots });
      } catch (error) { UI.err(error, T('保存失败')); }
    }

    // ---- 引擎状态 ----
    const engineCard = U.el('div', { class: 'card' });
    engineCard.appendChild(U.el('h2', {}, [
      U.el('span', { html: Icons.svg('cpu', { size: 17 }) }),
      U.el('span', { text: T('引擎状态') }),
    ]));
    const enginesData = State.engines || {};
    const builtin = enginesData.builtin || {};
    const tesseract = (enginesData.engines || {}).tesseract || {};
    const remote = (enginesData.engines || {}).remote || {};
    const table = U.el('table', { class: 'data' }, [
      U.el('thead', {}, [U.el('tr', {}, [
        U.el('th', { text: T('能力') }), U.el('th', { text: T('状态') }), U.el('th', { text: '' }),
      ])]),
    ]);
    const tbody = U.el('tbody', {});
    [
      [T('PDF 文字层提取（内置解析器）'), builtin.detail || T('无外部依赖'), true],
      [T('图片尺寸与 EXIF（内置）'), T('内置实现，始终可用'), true],
      [T('图片 OCR · tesseract'), tesseract.detail || T('未检测到'), !!tesseract.available],
      [T('图片 OCR · 远程接口'), remote.detail || T('未配置'), !!remote.available],
    ].forEach(([name, detail, ok]) => {
      tbody.appendChild(U.el('tr', {}, [
        U.el('td', { text: name }),
        U.el('td', { class: 'small muted', text: detail }),
        U.el('td', {}, [UI.badge(ok ? T('可用') : T('不可用'), ok ? 'ok' : 'neutral')]),
      ]));
    });
    table.appendChild(tbody);
    engineCard.appendChild(table);
    if (tesseract.languages && tesseract.languages.length) {
      engineCard.appendChild(U.el('div', { class: 'small muted mt1',
        text: T('tesseract 语言包：') + tesseract.languages.join('、') }));
    }
    host.appendChild(engineCard);

    // ---- 远程接口配置（密钥不回显）----
    const remoteCard = U.el('div', { class: 'card' });
    remoteCard.appendChild(U.el('h2', {}, [
      U.el('span', { html: Icons.svg('globe', { size: 17 }) }),
      U.el('span', { text: T('远程 OCR 接口（可选，默认关闭）') }),
    ]));
    remoteCard.appendChild(UI.banner('warn', T('启用后图片会被发送到该地址'),
      T('本应用的 PDF 提取与图片元数据完全离线。只有你在这里显式启用并填了地址之后，') +
      T('图片 OCR 才会把图片 POST 给该服务。<b>不配置则始终离线。</b>')));

    const enabled = U.el('input', { type: 'checkbox' });
    enabled.checked = !!remote.enabled;
    const endpoint = U.el('input', { type: 'text', placeholder: 'https://example.com/ocr',
                                     value: remote.endpoint || '' });
    const timeout = U.el('input', { type: 'number', min: '5', max: '600',
                                    value: String(remote.timeout || 30) });
    const apiKey = U.el('input', {
      type: 'password', placeholder: remote.key_set
        ? T('已保存（') + (remote.key_hint || '') + T('），留空则不修改') : T('未设置'),
    });
    const language = U.el('input', { type: 'text', placeholder: T('例如 chi_sim（可留空）') });

    const localLang = U.el('input', {
      type: 'text', placeholder: T('例如 chi_sim+eng（留空 = tesseract 默认）'),
      value: (State.engines && State.engines.engines
              && State.engines.engines.tesseract
              && State.engines.engines.tesseract.configured_language) || '',
    });

    remoteCard.appendChild(U.el('div', { class: 'checks mb1' }, [
      U.el('label', {}, [
        enabled,
        U.el('span', {
          text: T('启用远程 OCR（默认关闭；启用即表示你同意把图片发送到下面的地址）'),
        }),
      ]),
    ]));
    remoteCard.appendChild(U.el('div', { class: 'row' }, [
      U.el('label', { class: 'field' }, [
        U.el('span', { class: 'label-text', text: T('接口地址（POST，multipart）') }), endpoint,
      ]),
      U.el('label', { class: 'field' }, [
        U.el('span', { class: 'label-text', text: T('超时（秒）') }), timeout,
      ]),
    ]));
    remoteCard.appendChild(U.el('div', { class: 'row' }, [
      U.el('label', { class: 'field' }, [
        U.el('span', { class: 'label-text', text: T('API Key（存 secrets.json，权限 600）') }),
        apiKey,
      ]),
      U.el('label', { class: 'field' }, [
        U.el('span', { class: 'label-text', text: T('语言参数（可选）') }), language,
      ]),
    ]));
    remoteCard.appendChild(U.el('div', { class: 'small faint', text:
      T('密钥单独存放在 data/config/secrets.json，权限 600；接口响应里**不会**回显它。') +
      T('接口需返回 JSON：{"text": "识别结果"}。') }));

    const saveRemote = U.el('button', { class: 'btn primary' }, [
      U.el('span', { html: Icons.svg('save', { size: 15 }) }),
      U.el('span', { text: T('保存远程接口配置') }),
    ]);
    saveRemote.addEventListener('click', async () => {
      const payload = {
        enabled: enabled.checked, endpoint: endpoint.value.trim(),
        timeout: Number(timeout.value) || 30, language: language.value.trim(),
      };
      if (apiKey.value) payload.api_key = apiKey.value;
      try {
        const data = await API.post('api/ocr/remote', payload);
        State.engines = Object.assign({}, State.engines, data.engines);
        apiKey.value = '';
        UI.ok(T('已保存'), T('密钥不会被回显；需要修改时重新填写即可'));
        Shell.show('settings');
      } catch (error) { UI.err(error, T('保存失败')); }
    });
    const clearKey = U.el('button', { class: 'btn danger' }, [
      U.el('span', { html: Icons.svg('trash', { size: 15 }) }),
      U.el('span', { text: T('清除已保存的密钥') }),
    ]);
    clearKey.addEventListener('click', async () => {
      const confirmed = await UI.confirm({
        title: T('清除远程接口密钥'), danger: true,
        body: T('将删除 data/config/secrets.json 里的 API Key。')
              + T('清除后远程 OCR 会因缺少凭据而失败（除非该服务不需要密钥）。'),
        confirmText: T('清除'),
      });
      if (!confirmed) return;
      try {
        await API.post('api/ocr/remote', { clear_key: true });
        UI.ok(T('密钥已清除'));
        Shell.show('settings');
      } catch (error) { UI.err(error); }
    });
    const localLangRow = U.el('div', { class: 'row' }, [
      U.el('label', { class: 'field' }, [
        U.el('span', { class: 'label-text',
          text: T('本机 tesseract 语言（仅影响本机 OCR）') }), localLang,
      ]),
    ]);
    const saveLang = U.el('button', { class: 'btn' }, [
      U.el('span', { html: Icons.svg('save', { size: 15 }) }),
      U.el('span', { text: T('保存本机语言') }),
    ]);
    saveLang.addEventListener('click', async () => {
      try {
        await API.post('api/settings', { ocr_language: localLang.value.trim() });
        UI.ok(T('已保存'));
      } catch (error) { UI.err(error, T('保存失败')); }
    });
    remoteCard.appendChild(localLangRow);
    remoteCard.appendChild(U.el('div', { class: 'btn-row mt1' }, [saveRemote, clearKey, saveLang]));
    host.appendChild(remoteCard);

    // ---- 维护 ----
    const maintCard = U.el('div', { class: 'card' });
    maintCard.appendChild(U.el('h2', {}, [
      U.el('span', { html: Icons.svg('settings', { size: 17 }) }),
      U.el('span', { text: T('维护与关于') }),
    ]));
    const indexBtn = U.el('button', { class: 'btn' }, [
      U.el('span', { html: Icons.svg('scan', { size: 15 }) }),
      U.el('span', { text: T('重建索引（对已授权目录）') }),
    ]);
    indexBtn.addEventListener('click', () => {
      UI.pickDir({
        title: T('选择要建立索引的目录'),
        start: State.roots[0] || '',
        onPick: async (path) => {
          try {
            await Jobs.submit('index', { roots: [path] }, T('建立索引 ') + baseName(path));
            Shell.show('jobs');
          } catch (error) { UI.err(error); }
        },
      });
    });

    const aboutBtn = U.el('button', { class: 'btn' }, [
      U.el('span', { html: Icons.svg('info', { size: 15 }) }),
      U.el('span', { text: T('关于与隐私') }),
    ]);
    aboutBtn.addEventListener('click', async () => {
      let info = {};
      try { info = await API.get('api/app'); } catch (error) { /* 用默认值 */ }
      UI.modal({
        title: T('关于'), icon: 'info', wide: true,
        bodyHtml: `
          <p><b>${T('文档文字识别')}</b> v${U.esc(info.version || '')}</p>
          <p class="small muted">${T('离线提取 PDF 的文字层与图片元数据，输出 TXT / Markdown / JSON， 并可建立本地检索索引。')}</p>
          <h3 class="mt2">${T('隐私')}</h3>
          <ul class="small">
            <li>${T('不联网、不上传任何文件或元数据（远程 OCR 默认关闭，需管理员显式启用）')}</li>
            <li>${T('不收集使用统计、遥测或设备标识')}</li>
            <li>${T('源文件全程只读，不会被修改或删除')}</li>
            <li>${T('日志写入前已对密码 / Token / API Key 做脱敏')}</li>
          </ul>
          <h3 class="mt2">${T('运行时写入位置')}</h3>
          <pre class="logview small">${U.esc(info.paths ? JSON.stringify(info.paths, null, 2) : '')}</pre>
          <p class="small muted mt1">${T('完整清单见包内 README.md 的 「运行时写入路径清单（指引 12.9.6）」，隐私政策见 PRIVACY.md。')}</p>`,
        buttons: [{ text: T('关闭') }],
      });
    });
    maintCard.appendChild(U.el('div', { class: 'btn-row' }, [indexBtn, aboutBtn]));
    host.appendChild(maintCard);
  }

  /* ------------------------------------------------------------ 工具 */

  function baseName(path) {
    if (!path) return '';
    const parts = String(path).split(/[\\/]/);
    return parts[parts.length - 1] || path;
  }

  function dirName(path) {
    if (!path) return '';
    const cleaned = String(path).replace(/[\\/]+$/, '');
    const index = Math.max(cleaned.lastIndexOf('/'), cleaned.lastIndexOf('\\'));
    return index > 0 ? cleaned.slice(0, index) : '';
  }

  /* ------------------------------------------------------------ 启动 */

  async function boot() {
    Jobs.mountTaskbar(U.byId('taskbar'));
    Jobs.start(2500);

    const shell = Shell.init({
      extract: { label: T('识别'), icon: 'fileText', render: renderExtract },
      results: { label: T('结果'), icon: 'list', render: renderResults },
      search: { label: T('检索'), icon: 'search', render: renderSearch },
      jobs: { label: T('任务'), icon: 'activity', render: renderJobs },
      settings: { label: T('设置'), icon: 'settings', render: renderSettings },
    }, { defaultView: 'extract' });
    // 切语言后重渲染当前视图 —— 框架只换静态文案，动态渲染的部分要靠这个事件
    Shell.bindLanguage(shell);
    window.Shell = shell;

    try {
      const [engineData, settings] = await Promise.all([
        API.get('api/ocr/engine'),
        API.get('api/settings'),
      ]);
      State.engines = engineData;
      State.roots = (settings.settings && settings.settings.allowed_roots) || [];
      const tesseract = (engineData.engines || {}).tesseract || {};
      const remote = (engineData.engines || {}).remote || {};
      const meta = U.byId('engine-meta');
      if (meta) {
        meta.textContent = T('PDF 提取可用（离线）')
          + (tesseract.available ? T(' · tesseract 可用') : '')
          + (remote.available ? T(' · 远程接口已启用') : '');
      }
      await Shell.loadAppInfo();
    } catch (error) {
      const meta = U.byId('engine-meta');
      if (meta) meta.textContent = T('服务未就绪');
    }

    // 状态就绪后重渲染当前视图（首屏渲染时 engines/roots 还是空的）
    shell.show(shell.current() || 'extract');
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
