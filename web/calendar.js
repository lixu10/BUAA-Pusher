const WEEK_HOUR_HEIGHT = 56;
const weekDisplay = {key:null, early:false, late:false, comfortable:false, selectedDay:null};
function weekPinned(event) { return event.kind === "assignment" || event.all_day; }
function weekPinnedDate(event) { return dateOf(event.kind === "assignment" ? event.due_at || anchor(event) : anchor(event)); }
function weekDays() { const start=startOfWeek(state.cursor); return Array.from({length:7},(_,i)=>addDays(start,i)); }
function weekBounds() {
  const days=weekDays(), key=days[0].toISOString();
  if (weekDisplay.key!==key) {
    weekDisplay.key=key; weekDisplay.early=false; weekDisplay.late=false; weekDisplay.selectedDay=null;
    state.weekScrollTop=0; state.weekScrollLeft=0;
  }
  const layouts=days.flatMap(day=>weekDayLayout(day));
  const early=layouts.some(item=>item.startHour<8), late=layouts.some(item=>item.endHour>22);
  const startHour=early||weekDisplay.early?0:8, endHour=late||weekDisplay.late?24:22;
  const hasPinned=days.some(day=>visibleCalendarEvents().some(event=>weekPinned(event)&&sameDay(weekPinnedDate(event),day)));
  const reserve=48+20+(hasPinned?84:0)+(startHour>0?30:0)+(endHour<24?30:0);
  const available=$('#calendar-canvas').clientHeight||600;
  const compactHeight=Math.max(32,Math.min(48,Math.floor((available-reserve)/(endHour-startHour))));
  return {startHour,endHour,hourHeight:weekDisplay.comfortable?56:compactHeight,early,late};
}

// Keep actual durations separate from painted heights. Cross-day events are clipped
// to the visible day; overlapping cards receive lanes without modifying school data.
function weekDayLayout(day, {startHour=0,endHour=24,hourHeight=WEEK_HOUR_HEIGHT,minHeight=42}={}) {
  const next=addDays(day,1), gridHeight=(endHour-startHour)*hourHeight;
  const items=visibleCalendarEvents().filter(event=>!weekPinned(event)).flatMap(event=>{
    const start=eventDate(event); if(!start)return [];
    const givenEnd=dateOf(event.ends_at);
    const end=givenEnd&&givenEnd>start?givenEnd:new Date(start.getTime()+45*60000);
    if(start>=next||end<=day)return [];
    const startInDay=Math.max(0,(start-day)/3600000), endInDay=Math.min(24,(end-day)/3600000);
    if(endInDay<=startHour||startInDay>=endHour)return [];
    const top=Math.max(0,(startInDay-startHour)*hourHeight), actualBottom=Math.min(gridHeight,(endInDay-startHour)*hourHeight);
    const height=Math.min(Math.max(minHeight,actualBottom-top),gridHeight), paintedTop=Math.min(top,gridHeight-height);
    return [{event,top:paintedTop,height,bottom:paintedTop+height,startHour:startInDay,endHour:endInDay,continued:start<day||end>next}];
  }).sort((a,b)=>a.top-b.top||b.height-a.height);
  let group=[],groupEnd=-1;
  const finish=()=>{
    const ends=[];
    for(const item of group){let lane=ends.findIndex(end=>end<=item.top);if(lane<0)lane=ends.length;ends[lane]=item.bottom;item.lane=lane;}
    group.forEach(item=>item.lanes=ends.length);group=[];
  };
  for(const item of items){if(item.top>=groupEnd)finish();group.push(item);groupEnd=Math.max(groupEnd,item.bottom);}
  finish();return items;
}

function renderMobileWeek(days) {
  const selected=weekDisplay.selectedDay??((state.cursor.getDay()||7)-1), day=days[selected];
  const strip=days.map((date,index)=>{
    const count=weekDayLayout(date).length+visibleCalendarEvents().filter(event=>weekPinned(event)&&sameDay(weekPinnedDate(event),date)).length;
    return `<button type="button" data-week-day="${index}" class="${index===selected?'selected':''} ${sameDay(date,new Date())?'is-today':''}" aria-pressed="${index===selected}" aria-label="${fmtDate(date,{month:'numeric',day:'numeric',weekday:'long'})}，${count}项"><span>${'一二三四五六日'[index]}</span><b>${date.getDate()}</b><small>${count||'—'}</small></button>`;
  }).join('');
  const pinned=visibleCalendarEvents().filter(event=>weekPinned(event)&&sameDay(weekPinnedDate(event),day)).sort((a,b)=>weekPinnedDate(a)-weekPinnedDate(b));
  const timed=weekDayLayout(day).map(item=>item.event);
  const rows=[...pinned,...timed].map(event=>{
    const details=eventDetails(event,{includeAttendance:false});
    return `<button type="button" class="mobile-day-event ${weekPinned(event)?'deadline':'lesson'}" data-calendar-event="${event.id}"><span class="mobile-event-time">${weekPinned(event)?event.all_day&&event.kind!=='assignment'?'全天':fmtTime(event.due_at||anchor(event)):eventTimeRange(event)}</span><span class="mobile-event-main"><strong>${esc(event.title)}</strong>${details?`<small>${esc(details)}</small>`:''}<small>${esc(sourceLabel(event.source))} · ${esc(statusLabel(event))}</small>${attendanceBadge(event)}</span><i class="bi bi-chevron-right" aria-hidden="true"></i></button>`;
  }).join('');
  return `<div class="mobile-week"><div class="mobile-week-strip" role="group" aria-label="选择本周日期">${strip}</div><div class="mobile-day-heading"><strong>${fmtDate(day,{month:'long',day:'numeric',weekday:'long'})}</strong><span>${pinned.length+timed.length} 项</span></div><div class="mobile-day-list">${rows||'<div class="empty"><i class="bi bi-calendar-check"></i><strong>当天没有日程</strong></div>'}</div></div>`;
}

function renderWeek() {
  const days=weekDays(), now=new Date(), bounds=weekBounds(), {startHour,endHour,hourHeight}=bounds;
  const heads=days.map(d=>`<span class="${sameDay(d,now)?'is-today':''}">${'一二三四五六日'[(d.getDay()||7)-1]}<b>${d.getDate()}</b></span>`).join('');
  const axis=Array.from({length:endHour-startHour},(_,i)=>`<span>${String(i+startHour).padStart(2,'0')}:00</span>`).join('')+`<span class="time-end">${endHour}:00</span>`;
  const hasPinned=days.some(day=>visibleCalendarEvents().some(event=>weekPinned(event)&&sameDay(weekPinnedDate(event),day)));
  const pinned=days.map(day=>{
    const items=visibleCalendarEvents().filter(event=>weekPinned(event)&&sameDay(weekPinnedDate(event),day)).sort((a,b)=>weekPinnedDate(a)-weekPinnedDate(b));
    return `<div class="week-pinned-day">${items.map(event=>`<button type="button" class="week-deadline" data-calendar-event="${event.id}" title="${esc(event.title)}"><time>${event.all_day&&event.kind!=='assignment'?'全天':fmtTime(event.due_at||anchor(event))}</time><strong>${esc(event.title)}</strong></button>`).join('')||'<span class="week-no-deadline">—</span>'}</div>`;
  }).join('');
  const columns=days.map(day=>{
    const items=weekDayLayout(day,{...bounds,minHeight:36}).map(({event,top,height,lane,lanes,continued})=>{
      const details=eventDetails(event), density=height<54?'compact':height<94?'medium':'full';
      const tooltip=[event.title,eventTimeRange(event),details,statusLabel(event),attendanceLabel(event)].filter(Boolean).join('\n');
      return `<button type="button" class="calendar-event ${density} ${event.kind==='boya'?'boya-event':''}" data-calendar-event="${event.id}" title="${esc(tooltip)}" aria-label="${esc(tooltip)}" style="top:${top}px;height:${height}px;left:calc(${lane/lanes*100}% + 3px);width:calc(${100/lanes}% - 6px)"><strong>${esc(event.title)}</strong><span class="calendar-event-time">${eventTimeRange(event)}${continued?' · 跨日':''}</span>${details?`<span class="calendar-event-meta">${esc(details)}</span>`:''}<span class="calendar-event-status">${esc(statusLabel(event))}${attendanceLabel(event)?` · ${esc(attendanceLabel(event))}`:''}</span></button>`;
    }).join('');
    const hour=now.getHours()+now.getMinutes()/60;
    const current=sameDay(day,now)&&hour>=startHour&&hour<endHour?`<div class="week-now" style="top:${(hour-startHour)*hourHeight}px" aria-label="当前时间 ${fmtTime(now)}"></div>`:'';
    return `<div class="day-column">${items}${current}</div>`;
  }).join('');
  const earlyFold=startHour>0?'<button type="button" class="week-fold" data-week-expand="early"><i class="bi bi-chevron-expand"></i>00:00–08:00 · 展开</button>':'';
  const lateFold=endHour<24?'<button type="button" class="week-fold" data-week-expand="late"><i class="bi bi-chevron-expand"></i>22:00–24:00 · 展开</button>':'';
  return `<div class="desktop-week" style="--grid-hour:${hourHeight}px"><div class="week-sticky"><div class="week-head"><span>时间</span>${heads}</div>${hasPinned?`<div class="week-pinned"><span class="week-pinned-label">截止<br>／全天</span>${pinned}</div>`:''}${earlyFold}</div><div class="week-body" style="height:${(endHour-startHour)*hourHeight}px"><div class="time-axis">${axis}</div>${columns}</div>${lateFold}</div>${renderMobileWeek(days)}`;
}

function bindWeekControls() {
  let resizeTimer;
  window.addEventListener('resize',()=>{clearTimeout(resizeTimer);resizeTimer=setTimeout(()=>{if(state.page==='calendar'&&state.view==='week')renderCalendar();},150);});
  $('#calendar-canvas').addEventListener('click',event=>{
    const day=event.target.closest('[data-week-day]'), expand=event.target.closest('[data-week-expand]');
    if(day){weekDisplay.selectedDay=Number(day.dataset.weekDay);state.cursor=weekDays()[weekDisplay.selectedDay];renderCalendar();}
    if(expand){weekDisplay[expand.dataset.weekExpand]=true;renderCalendar({resetScroll:true});}
  });
  $('#week-density').addEventListener('click',()=>{weekDisplay.comfortable=!weekDisplay.comfortable;renderCalendar({resetScroll:true});});
  $('#week-reset-hours').addEventListener('click',()=>{weekDisplay.early=false;weekDisplay.late=false;renderCalendar({resetScroll:true});});
  $$('[data-week-hour]').forEach(button=>button.addEventListener('click',()=>{
    const hour=Number(button.dataset.weekHour);weekBounds();
    if(hour<8)weekDisplay.early=true;if(hour>=22)weekDisplay.late=true;
    renderCalendar();const range=weekBounds();state.weekScrollTop=(hour-range.startHour)*range.hourHeight;$('#calendar-canvas').scrollTop=state.weekScrollTop;
  }));
}
