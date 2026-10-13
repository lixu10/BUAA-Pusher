function localEventInput(value, allDay=false) {
  const date=dateOf(value);
  return date ? new Date(date.getTime()-date.getTimezoneOffset()*60000).toISOString().slice(0,allDay?10:16) : '';
}
function customReminderPoint(timing, days=0, hours=0, minutes=0) {
  if (!['before_start','at_start','before_end','at_end'].includes(timing)) throw new Error('请选择提醒时机');
  return {anchor:timing.endsWith('end')?'ends_at':'starts_at',offset_minutes:timing.startsWith('at_')?0:-advanceMinutes({days,hours,minutes})};
}
function customReminderSummary(rule) {
  const times=(rule.triggers||[]).map(point=>`${point.anchor==='ends_at'?'结束':'开始'}${point.offset_minutes<0?'前'+durationText(point.offset_minutes):'时'}`);
  const channels=(rule.actions||[]).map(action=>channelLabels[action.channel]||action.channel);
  return `${times.join('、')}提醒${channels.length?' · '+channels.join('、'):''}`;
}
function customEventDetailActions(event) {
  if (event.source!=='custom' || event.kind!=='custom') return '';
  const rule=state.data.rules.find(item=>item.profile==='custom-event'&&item.scope.event_ids?.includes(event.id));
  return `<div class="custom-detail-actions">${rule?`<span>${esc(rule.enabled?customReminderSummary(rule):'提醒已停用')}</span>`:'<span>未设置提醒</span>'}<button type="button" class="quiet-button" data-edit-custom-event="${event.id}"><i class="bi bi-pencil"></i>编辑日程</button></div>`;
}
function bindCustomDetailActions() {
  $$('[data-edit-custom-event]').forEach(button=>button.addEventListener('click',()=>openCustomEventDialog(Number(button.dataset.editCustomEvent))));
}
function toggleCustomReminder() {
  const enabled=$('#event-form').elements.with_reminder.checked, fieldset=$('#custom-reminder-settings');
  fieldset.hidden=!enabled; fieldset.disabled=!enabled;
}
function toggleCustomAllDay() {
  const form=$('#event-form'), allDay=form.elements.all_day.checked;
  for (const name of ['starts_at','ends_at']) {
    const input=form.elements[name], previous=input.value;
    input.type=allDay?'date':'datetime-local'; input.step=allDay?'1':'60';
    input.value=previous?allDay?previous.slice(0,10):previous.length===10?`${previous}T${name==='starts_at'?'09:00':'10:00'}`:previous:'';
  }
  $('#custom-end-label').textContent=allDay?'结束日期（含当天）':'结束（可选）';
}
function addCustomReminderTime(point={anchor:'starts_at',offset_minutes:-15}) {
  if ($$('#custom-reminder-times .custom-reminder-time-row').length>=10) { toast('最多设置 10 个提醒时间'); return; }
  const timing=(point.offset_minutes===0?'at_':'before_')+(point.anchor==='ends_at'?'end':'start'), units=advancePoint(Math.abs(point.offset_minutes));
  const row=document.createElement('div'); row.className='custom-reminder-time-row';
  row.innerHTML=`<select data-custom-timing aria-label="提醒时机">${[['before_start','开始前'],['at_start','开始时'],['before_end','结束前'],['at_end','结束时']].map(([value,label])=>`<option value="${value}" ${value===timing?'selected':''}>${label}</option>`).join('')}</select><div class="custom-reminder-offset">${[['days','天',365],['hours','小时',23],['minutes','分钟',59]].map(([key,label,max])=>`<label><input type="number" data-custom-unit="${key}" value="${units[key]}" min="0" max="${max}" step="1" required aria-label="提前${label}"><span>${label}</span></label>`).join('')}</div><button type="button" class="icon-button" aria-label="删除提醒时间"><i class="bi bi-x-lg"></i></button>`;
  const update=()=>{const at=row.querySelector('[data-custom-timing]').value.startsWith('at_'); row.querySelectorAll('[data-custom-unit]').forEach(input=>input.disabled=at); row.querySelector('.custom-reminder-offset').hidden=at;};
  row.querySelector('[data-custom-timing]').addEventListener('change',update); update();
  row.querySelector('button').addEventListener('click',()=>{if($$('#custom-reminder-times .custom-reminder-time-row').length>1)row.remove();});
  $('#custom-reminder-times').append(row);
}
function renderCustomChannels(actions=[{channel:'in_app'}]) {
  const selected=new Set(actions.map(action=>action.channel_id?`${action.channel}:${action.channel_id}`:action.channel));
  const options=[{value:'in_app',label:'站内通知'},...state.data.channels.filter(channel=>channel.enabled).map(channel=>({value:`${channel.type}:${channel.id}`,label:channel.name}))];
  $('#custom-reminder-channels').innerHTML=options.map(option=>`<label><input type="checkbox" data-custom-channel value="${esc(option.value)}" ${selected.has(option.value)?'checked':''}>${esc(option.label)}</label>`).join('');
}
async function openCustomEventDialog(id=null) {
  try {
    const event=Number.isInteger(id)?await api(`/api/events/${id}`):null, form=$('#event-form');
    form.reset(); $('#event-error').textContent=''; $('#custom-reminder-times').innerHTML='';
    form.elements.event_id.value=event?.id||'';
    $('#event-dialog-title').textContent=event?'编辑日程':'新建日程'; $('#delete-event-button').hidden=!event;
    let start=event?dateOf(event.starts_at):new Date(state.page==='calendar'?state.cursor:new Date());
    if (!event) {
      start.setHours(9,0,0,0);
      const now=new Date();
      if (sameDay(start,now)) {start=new Date(now);start.setMinutes(Math.ceil((start.getMinutes()+15)/15)*15,0,0);}
    }
    const end=event?dateOf(event.ends_at):new Date(start.getTime()+3600000);
    form.elements.title.value=event?.title||''; form.elements.location.value=event?.location||'';
    form.elements.description.value=event?.description||''; form.elements.all_day.checked=event?.all_day||false;
    toggleCustomAllDay();
    form.elements.starts_at.value=localEventInput(start,form.elements.all_day.checked);
    form.elements.ends_at.value=localEventInput(form.elements.all_day.checked&&end?addDays(end,-1):end,form.elements.all_day.checked);
    form.elements.with_reminder.checked=Boolean(event?.reminder);
    form.elements.reminder_enabled.checked=event?.reminder?.enabled??true;
    form.elements.reminder_quiet_policy.value=event?.reminder?.quiet_hours_policy||'delay';
    (event?.reminder?.times||[{anchor:'starts_at',offset_minutes:-15}]).forEach(addCustomReminderTime);
    renderCustomChannels(event?.reminder?.actions); toggleCustomReminder();
    if ($('#calendar-detail-dialog').open) $('#calendar-detail-dialog').close();
    $('#event-dialog').showModal(); $('.custom-event-body').scrollTop=0;
  } catch(error) {toast(error.message);}
}
function customEventPayload(form) {
  const allDay=form.elements.all_day.checked, startInput=form.elements.starts_at.value, endInput=form.elements.ends_at.value;
  const start=dateOf(allDay?`${startInput}T00:00:00`:startInput);
  if (!start) throw new Error('请设置开始时间');
  let end=endInput?dateOf(allDay?`${endInput}T00:00:00`:endInput):null;
  if (endInput&&!end) throw new Error('结束时间无效');
  if (allDay) end=addDays(end||start,1);
  if (end&&end<=start) throw new Error('结束时间必须晚于开始时间');
  const title=form.elements.title.value.trim(); if(!title)throw new Error('请填写日程名称');
  const payload={title,kind:'custom',starts_at:start.toISOString(),ends_at:end?.toISOString()||null,all_day:allDay,location:form.elements.location.value.trim()||null,description:form.elements.description.value,reminder:null};
  if (form.elements.with_reminder.checked) {
    const times=[...form.querySelectorAll('.custom-reminder-time-row')].map(row=>customReminderPoint(row.querySelector('[data-custom-timing]').value,...['days','hours','minutes'].map(key=>Number(row.querySelector(`[data-custom-unit="${key}"]`).value))));
    if(!times.length||times.length>10)throw new Error('请设置 1–10 个提醒时间');
    if(new Set(times.map(point=>point.anchor+':'+point.offset_minutes)).size!==times.length)throw new Error('提醒时间点不能重复');
    if(times.some(point=>point.anchor==='ends_at')&&!end)throw new Error('结束提醒需要设置结束时间');
    if(new Set(times.map(point=>(point.anchor==='ends_at'?end:start).getTime()+point.offset_minutes*60000)).size!==times.length)throw new Error('提醒时间点不能重复');
    const actions=[...form.querySelectorAll('[data-custom-channel]:checked')].map(input=>{const [channel,id]=input.value.split(':');return {channel,channel_id:id?Number(id):null,after_minutes:0};});
    if(!actions.length)throw new Error('至少选择一种通知方式');
    payload.reminder={enabled:form.elements.reminder_enabled.checked,times,actions,quiet_hours_policy:form.elements.reminder_quiet_policy.value};
  }
  return payload;
}
async function saveCustomEvent(event) {
  event.preventDefault(); const form=event.currentTarget, button=$('#save-event-button');
  if(button.disabled)return; button.disabled=true; $('#event-error').textContent='';
  let saved=false;
  try {
    const id=form.elements.event_id.value, payload=customEventPayload(form);
    const result=await api(id?`/api/events/${id}`:'/api/events',{method:id?'PUT':'POST',body:JSON.stringify(payload)});
    saved=true; $('#event-dialog').close(); form.reset();
    state.cursor=dateOf(result.starts_at); weekDisplay.selectedDay=null;
    await loadDashboard(); updatePeriodTitle(); toast(payload.reminder?'日程和提醒已保存':'日程已保存');
  } catch(error) {if(saved)toast('日程已保存，请刷新页面');else $('#event-error').textContent=error.message;}
  finally {button.disabled=false;}
}
async function deleteCustomEvent() {
  const id=$('#event-form').elements.event_id.value, button=$('#delete-event-button');
  if(!id||button.disabled||!confirm('删除这条日程及对应提醒？'))return;
  button.disabled=true;
  try {await api(`/api/events/${id}`,{method:'DELETE'});$('#event-dialog').close();await loadDashboard();toast('日程及对应提醒已删除');}
  catch(error){$('#event-error').textContent=error.message;}
  finally {button.disabled=false;}
}
function bindCustomEventControls() {
  $$('[data-new-event]').forEach(button=>button.addEventListener('click',()=>openCustomEventDialog()));
  $('#event-form').addEventListener('submit',saveCustomEvent);
  $('#event-form').elements.with_reminder.addEventListener('change',toggleCustomReminder);
  $('#event-form').elements.all_day.addEventListener('change',toggleCustomAllDay);
  $('#add-custom-reminder-time').addEventListener('click',()=>addCustomReminderTime());
  $('#delete-event-button').addEventListener('click',deleteCustomEvent);
}
