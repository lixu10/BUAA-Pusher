const boyaProfileNames = {
  'boya-course': '博雅课程提醒', 'boya-course-change': '已报名课程变更',
  'boya-new-course': '新课程提醒', 'boya-enrollment-soon': '报名即将开放',
  'boya-enrollment-open': '报名开放提醒', 'boya-seats': '名额回流提醒', 'boya-enrollment-closing': '报名即将截止',
};
const boyaCatalogProfiles = new Set(['boya-new-course','boya-enrollment-soon','boya-enrollment-open','boya-seats','boya-enrollment-closing']);
const boyaStateLabels = {preview:'未开放',available:'可报名',full:'已满',closed:'报名结束',started:'已开课',ended:'已结束',selected:'已报名',unknown:'状态未知'};
let boyaView = {tab:'chosen',page:1,data:null,loading:false,version:0};
function boyaEnabled() { return Boolean(state.data.sources.find(source=>source.id==='bykc')?.enabled); }
function boyaSignType(meta) { return ({'1':'仅签到','2':'签到＋签退'})[meta.sign_type] || '签到方式未知'; }
function boyaCampus(meta) { return meta.all_campuses ? '全部校区' : (meta.campus_names || []).join('、') || '校区未知'; }
function boyaOptions() { return state.data.boya_options || {categories:[],campuses:[],sign_types:[{value:'1',label:'仅签到'},{value:'2',label:'签到＋签退'}]}; }
function renderBoya() {
  const source = state.data.sources.find(source=>source.id==='bykc'), catalog = state.data.boya_catalog_state || {};
  $$('[data-boya-tab]').forEach(button=>button.classList.toggle('active',button.dataset.boyaTab===boyaView.tab));
  $('#boya-filters').hidden = boyaView.tab !== 'catalog';
  $('#boya-pager').hidden = boyaView.tab !== 'catalog';
  const key = boyaView.tab === 'catalog' ? catalog : source || {};
  $('#boya-sync').disabled = !boyaEnabled() || Boolean(key.syncing);
  $('#boya-sync').innerHTML = `<i class="bi bi-arrow-repeat"></i>${key.syncing ? '同步中' : boyaView.tab==='catalog' ? '同步课程列表' : '同步已报名'}`;
  const progress=key.progress || key.sync_progress;
  $('#boya-status').textContent = key.syncing ? `${progress?.phase || '同步中'}${progress?.total ? ` ${progress.completed}/${progress.total}` : ''}` : key.detail || (key.last_sync_at ? `更新于 ${fmtDate(dateOf(key.last_sync_at),{month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'})}` : '尚未同步');
  if (document.activeElement !== $('#boya-catalog-interval')) {
    const interval = catalog.refresh_interval_minutes ?? 5;
    const values = [...new Set([0,1,2,5,15,30,60,360,interval])].sort((a,b)=>a-b);
    $('#boya-catalog-interval').innerHTML = values.map(n=>`<option value="${n}" ${n===interval?'selected':''}>${n?`${n} 分钟`:'手动'}</option>`).join('');
  }
  const options = boyaOptions();
  for (const [selector,items,label] of [['#boya-category',options.categories,'全部分类'],['#boya-campus',options.campuses,'全部校区'],['#boya-sign-type',options.sign_types,'全部签到方式']]) {
    const input=$(selector),selected=input.value;
    input.innerHTML = `<option value="">${label}</option>`+items.map(item=>`<option value="${esc(item.value)}" ${item.value===selected?'selected':''}>${esc(item.label)}</option>`).join('');
  }
  let courses;
  if (boyaView.tab==='chosen') {
    courses = state.data.events.filter(event=>event.source==='bykc' && event.kind==='boya' && !event.ignored && ($('#boya-history').checked || !isExpired(event))).sort((a,b)=>(eventDate(a)?.getTime()??Infinity)-(eventDate(b)?.getTime()??Infinity));
    $('#boya-count').textContent = `${courses.length} 门`;
  } else {
    courses = boyaView.data?.courses || [];
    $('#boya-count').textContent = `${boyaView.data?.total ?? 0} 门`;
    const pages=Math.max(1,Math.ceil((boyaView.data?.total || 0)/20));
    $('#boya-page-number').textContent = `${boyaView.page} / ${pages}`;
    $('#boya-page-prev').disabled = boyaView.page<=1;
    $('#boya-page-next').disabled = boyaView.page>=pages;
  }
  $('#boya-history-row').hidden = boyaView.tab!=='chosen';
  $('#boya-list').innerHTML = courses.length ? courses.map(event=>{
    const meta=event.metadata || {}, selected=boyaView.tab==='chosen'||meta.selected===true;
    const status=selected?statusLabel(event):boyaStateLabels[event.enrollment_state] || '状态未知';
    return `<article class="boya-row"><div class="boya-row-main"><button class="event-detail-link" type="button" data-boya-detail="${esc(meta.course_id)}">${esc(event.title)}</button><div class="boya-tags"><span>${esc((meta.category_names||[]).join(' / '))}</span><span>${esc(boyaCampus(meta))}</span><span>${esc(boyaSignType(meta))}</span></div><div class="boya-course-time"><i class="bi bi-calendar3"></i>${detailDate(event.starts_at)||'时间未知'}${event.ends_at ? ` — ${fmtTime(event.ends_at)}`:''}</div><small>${esc(meta.teacher || '教师未提供')}${event.location ? `　${esc(event.location)}`:''}</small></div><div class="boya-enrollment"><span class="boya-status-label ${event.enrollment_state==='available'?'available':''}">${esc(status)}</span>${!selected?`<strong>${meta.remaining===null||meta.remaining===undefined?'余量未知':`余量 ${esc(meta.remaining)}`}</strong><small>${meta.current_count ?? '—'} / ${meta.max_count ?? '—'} 人</small><small>开放 ${detailDate(meta.enrollment_starts_at)||'未知'}</small><small>截止 ${detailDate(meta.enrollment_ends_at)||'未知'}</small>`:''}</div><div class="boya-actions"><button type="button" class="quiet-button" data-boya-detail="${esc(meta.course_id)}">详情</button>${!selected?`<button type="button" class="quiet-button" data-boya-watch="${esc(meta.course_id)}" data-title="${esc(event.title)}"><i class="bi bi-bell"></i>提醒</button>`:''}</div></article>`;
  }).join('') : `<div class="empty"><i class="bi bi-compass"></i><strong>${boyaView.loading?'读取课程…':boyaView.tab==='chosen'?'暂无已报名课程':'暂无符合条件的课程'}</strong>${!boyaEnabled()?'<button class="quiet-button" type="button" data-boya-connect>启用博雅连接</button>':''}</div>`;
  $$('[data-boya-detail]').forEach(button=>button.addEventListener('click',()=>openBoyaDetail(button.dataset.boyaDetail)));
  $$('[data-boya-watch]').forEach(button=>button.addEventListener('click',()=>openRuleDialog({profile:'boya-enrollment-open',name:`${button.dataset.title} 报名开放`,profile_settings:{course_ids:[button.dataset.boyaWatch]},actions:[{channel:'in_app'}],quiet_hours_policy:'ignore'})));
  $$('[data-boya-connect]').forEach(button=>button.addEventListener('click',()=>navigate('connections')));
}
async function loadBoyaCatalog() {
  const version=++boyaView.version; boyaView.loading=true;
  const query=new URLSearchParams({page:String(boyaView.page),size:'20',search:$('#boya-search').value,category:$('#boya-category').value,campus:$('#boya-campus').value,sign_type:$('#boya-sign-type').value,status:$('#boya-filter-state').value});
  try { const data=await api(`/api/boya/courses?${query}`); if(version!==boyaView.version)return; boyaView.data=data; }
  catch(error) { toast(error.message); }
  finally { if(version===boyaView.version){boyaView.loading=false;renderBoya();} }
}
async function syncBoyaComponent() {
  try { await api(boyaView.tab==='catalog'?'/api/boya/catalog/sync':'/api/sources/bykc/sync',{method:'POST'}); await loadDashboard(); scheduleSyncPoll(); }
  catch(error){toast(error.message);}
}
function renderBoyaDetail(event) {
  const meta=event.metadata || {}, windows=meta.sign_windows || {};
  const fields=[['分类',(meta.category_names||[]).join(' / ')],['适用校区',boyaCampus(meta)],['签到方式',boyaSignType(meta)],['报名开始',detailDate(meta.enrollment_starts_at)],['报名截止',detailDate(meta.enrollment_ends_at)],['退选截止',detailDate(meta.cancel_ends_at)],['余量',meta.remaining],['已报名人数',meta.current_count],['人数上限',meta.max_count],['报名时间',detailDate(meta.enrolled_at)],['联系人',meta.contact],['联系电话',meta.contact_phone],['开课单位',meta.department],['考核',meta.pass===1?'已通过':meta.pass===0?'未通过／未完成':null],['得分',meta.score],['考勤状态',meta.checkin===0?'未签到':meta.checkin===1?'已签到':meta.checkin!==null&&meta.checkin!==undefined?`学校状态 ${meta.checkin}`:null]];
  let html=`<dl class="boya-detail-fields">${detailFields(fields)}</dl>`;
  const signFields=detailFields([['签到开始',detailDate(windows.signStartDate)],['签到结束',detailDate(windows.signEndDate)],['签退开始',detailDate(windows.signOutStartDate)],['签退结束',detailDate(windows.signOutEndDate)],['学校备注',meta.sign_info]]);
  html+=detailDisclosure('签到／签退信息',signFields?`<dl>${signFields}</dl>`:'');
  const audience=Object.values(meta.audience||{}).flat().filter(value=>typeof value==='string').join('、');
  html+=detailDisclosure('适用人群',audience?`<div class="detail-long-text">${esc(audience)}</div>`:'');
  const homework=detailFields([['开放',detailDate(meta.homework_starts_at)],['截止',detailDate(meta.homework_ends_at)],['已提交内容',meta.homework],['附件名称',meta.homework_attachment_name]]);
  html+=detailDisclosure('作业信息',meta.homework_description||homework?`<dl>${homework}</dl>${meta.homework_description?`<div class="detail-long-text">${esc(meta.homework_description)}</div>`:''}`:'');
  return html;
}
async function openBoyaDetail(id, refresh=false) {
  try {
    const event=await api(`/api/boya/courses/${encodeURIComponent(id)}${refresh?'/refresh':''}`,refresh?{method:'POST'}:{});
    $('#calendar-detail-title').textContent=event.title;
    $('#calendar-detail-body').innerHTML=renderEventDetail(event)+`<div class="boya-detail-actions"><button type="button" class="quiet-button" id="boya-refresh-detail" ${boyaEnabled()?'':'disabled'}><i class="bi bi-arrow-repeat"></i>刷新详情</button><a class="quiet-button" href="https://bykc.buaa.edu.cn/system/course-select" target="_blank" rel="noopener noreferrer">学校报名页面<i class="bi bi-box-arrow-up-right"></i></a></div>`;
    $('#boya-refresh-detail').addEventListener('click',()=>openBoyaDetail(id,true));
    if(!$('#calendar-detail-dialog').open) $('#calendar-detail-dialog').showModal();
  } catch(error){toast(error.message);}
}
function renderBoyaRuleSettings(profile, settings) {
  const options=boyaOptions();
  const checks=(key,label,items)=>{
    const values=new Map(items.map(item=>[item.value,item.label]));
    (settings[key] || []).forEach(value=>{if(!values.has(value))values.set(value,value);});
    return `<div class="boya-filter-field"><span>${label}<small>未选则不限</small></span><div class="choice-grid">${[...values].map(([value,name])=>`<label><input type="checkbox" data-boya-filter="${key}" value="${esc(value)}" ${(settings[key]||[]).includes(value)?'checked':''}>${esc(name)}</label>`).join('')||'<span class="muted">暂无选项</span>'}</div></div>`;
  };
  let html=checks('categories','课程分类',options.categories)+checks('campuses','适用校区',options.campuses)+checks('sign_types','签到方式',options.sign_types);
  html+=`<label class="rule-field"><span>课程关键词</span><input data-boya-text="keywords" value="${esc((settings.keywords||[]).join('，'))}" placeholder="逗号分隔，可留空"></label><label class="rule-field"><span>排除关键词</span><input data-boya-text="exclude_keywords" value="${esc((settings.exclude_keywords||[]).join('，'))}"></label>`;
  if(settings.course_ids?.length)html+=`<label class="rule-field"><span>指定课程</span><input data-boya-text="course_ids" value="${esc(settings.course_ids.join('，'))}" aria-label="指定课程编号"></label>`;
  html+=`<label class="rule-field"><span>最低余量</span><input data-profile-setting="min_remaining" type="number" min="1" max="10000" value="${settings.min_remaining??1}"></label>`;
  if(profile==='boya-new-course')html+=`<label class="check-row"><input type="checkbox" data-boya-preview ${settings.include_preview!==false?'checked':''}><span>包含未开放课程</span></label>`;
  if(profile==='boya-seats')html+=`<label class="rule-field"><span>通知间隔（分钟）</span><input data-profile-setting="cooldown_minutes" type="number" min="0" max="1440" value="${settings.cooldown_minutes??15}"></label>`;
  if(['boya-enrollment-soon','boya-enrollment-closing'].includes(profile))html+=`<div class="reminder-time-list" id="boya-time-list">${(settings.advance_minutes||[10,1]).map(value=>reminderTimeRow(advancePoint(value),profile==='boya-enrollment-closing'?'报名截止前':'报名开放前')).join('')}</div><button type="button" class="quiet-button" id="add-boya-time"><i class="bi bi-plus"></i>添加时间</button>`;
  return html;
}
function collectBoyaSettings(settings) {
  for(const key of ['categories','campuses','sign_types']) settings[key]=$$(`[data-boya-filter="${key}"]:checked`).map(input=>input.value);
  $$('[data-boya-text]').forEach(input=>settings[input.dataset.boyaText]=[...new Set(input.value.split(/[,，;；\n]/).map(value=>value.trim()).filter(Boolean))]);
  settings.include_preview=Boolean($('[data-boya-preview]')?.checked);
  if($('#boya-time-list')) settings.advance_minutes=$$('#boya-time-list .reminder-time-row').map(row=>advanceMinutes(Object.fromEntries([...row.querySelectorAll('[data-advance]')].map(input=>[input.dataset.advance,Number(input.value)]))));
}
function bindBoyaEvents() {
  $$('[data-boya-tab]').forEach(button=>button.addEventListener('click',()=>{boyaView.tab=button.dataset.boyaTab;renderBoya();if(boyaView.tab==='catalog')loadBoyaCatalog();}));
  $('#boya-sync').addEventListener('click',syncBoyaComponent);
  $('#boya-history').addEventListener('change',renderBoya);
  for(const selector of ['#boya-category','#boya-campus','#boya-sign-type','#boya-filter-state']) $(selector).addEventListener('change',()=>{boyaView.page=1;loadBoyaCatalog();});
  let searchTimer;
  $('#boya-search').addEventListener('input',()=>{clearTimeout(searchTimer);searchTimer=setTimeout(()=>{boyaView.page=1;loadBoyaCatalog();},250);});
  $('#boya-page-prev').addEventListener('click',()=>{boyaView.page--;loadBoyaCatalog();});
  $('#boya-page-next').addEventListener('click',()=>{boyaView.page++;loadBoyaCatalog();});
  $('#boya-catalog-interval').addEventListener('change',async event=>{try{await api('/api/boya/catalog/settings',{method:'PATCH',body:JSON.stringify({refresh_interval_minutes:Number(event.target.value)})});await loadDashboard();}catch(error){toast(error.message);}});
  $('#profile-settings').addEventListener('click',event=>{if(event.target.closest('#add-boya-time')){if($$('#boya-time-list .reminder-time-row').length>=10)return;$('#boya-time-list').insertAdjacentHTML('beforeend',reminderTimeRow(advancePoint(5),$('#rule-profile').value==='boya-enrollment-closing'?'报名截止前':'报名开放前'));}});
}
