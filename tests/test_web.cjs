const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

test('account auth refresh settings default to 24 hours and save separately from sync intervals',()=>{
  const html=fs.readFileSync(path.join(__dirname,'../web/index.html'),'utf8');
  const js=fs.readFileSync(path.join(__dirname,'../web/app.js'),'utf8');
  assert.match(html,/id="auth-refresh-form"/);
  assert.match(html,/name="auth_refresh_hours"[^>]*value="24"/);
  assert.match(html,/min="1" max="720"/);
  assert.match(js,/JSON.stringify\(\{ auth_refresh_hours: hours \}\)/);
  assert.match(js,/authForm.contains\(document.activeElement\)/);
});

test('school connections offer campus direct and school WebVPN with a dedicated login action', () => {
  const ui = app();
  const base = {id:'judge',label:'JUDGE 作业',available:true,enabled:true,status:'healthy',event_count:1};
  ui.context.fixtureSource = base;
  let html = ui.run('renderSourceRow(fixtureSource)');
  assert.match(html, /data-source-mode="judge"/);
  assert.match(html, /直连（校内）/); assert.match(html, /学校 WebVPN/);
  assert.match(html, /value="direct" selected/);
  ui.context.fixtureSource = {...base, network_mode:'webvpn',webvpn_login_required:true};
  ui.run('state.school = {school_id:"12345678",remember_password:false}');
  html = ui.run('renderSourceRow(fixtureSource)');
  assert.match(html, /value="webvpn" selected/);
  assert.match(html, /data-source-webvpn-login/);
  assert.doesNotMatch(html, /data-source-sync="judge"/);
  ui.run('state.school.remember_password = true');
  assert.match(ui.run('renderSourceRow(fixtureSource)'), /data-source-sync="judge"/);
  ui.context.fixtureSource = {...base,network_mode:'webvpn',webvpn_login_required:true,status:'login_required'};
  assert.match(ui.run('renderSourceRow(fixtureSource)'), /data-source-webvpn-login/);
  ui.context.fixtureSource = {...base,id:'spoc'};
  assert.match(ui.run('renderSourceRow(fixtureSource)'), /data-source-mode="spoc"/);
  ui.context.fixtureSource = {...base,id:'grade',available:false};
  html = ui.run('renderSourceRow(fixtureSource)');
  assert.match(html, /data-source-mode="grade"/); assert.match(html, /Coming Soon/);
  assert.doesNotMatch(html, /data-source-sync/);
  ui.context.fixtureSource = {...base,network_mode:'webvpn',syncing:true};
  assert.match(ui.run('renderSourceRow(fixtureSource)'), /data-source-mode="judge"[^>]*disabled/);
});

test('attendance editor offers minute checkpoints, bounds and course exclusions', () => {
  const ui=app();
  ui.run('renderProfileSettings("signin")');
  const html=ui.elements.get('#profile-settings').innerHTML;
  assert.match(html,/add-check-time/); assert.match(html,/开始前/); assert.match(html,/结束前/);
  assert.match(html,/max="10"/); assert.match(html,/排除课程/);
  assert.doesNotMatch(html,/再次提醒|同步仅建立基线|completion-filter/);
  assert.equal(ui.run('checkTimePoint("before_start",10).offset_minutes'),-10);
  assert.equal(ui.run('checkTimePoint("before_end",5).anchor'),"ends_at");
  assert.equal(ui.run('checkTimePoint("at_end",0).offset_minutes'),0);
  assert.throws(()=>ui.run('checkTimePoint("before_start",11)'));
  assert.throws(()=>ui.run('checkTimePoint("after_start",1.5)'));
  ui.context.fixtureSource={id:'iclass',label:'课程签到',available:true,enabled:false};
  assert.match(ui.run('renderSourceRow(fixtureSource)'),/data-source-interval="iclass"/);
  assert.match(ui.run('renderSourceRow(fixtureSource)'),/5 分钟/);
  assert.match(ui.run('renderSourceRow(fixtureSource)'),/15 分钟/);
  assert.match(ui.run('renderSourceRow(fixtureSource)'),/手动/);
  ui.context.fixtureSource.refresh_interval_minutes=25;
  assert.match(ui.run('renderSourceRow(fixtureSource)'),/value="25" selected/);
});

test('attendance labels attach to courses without replacing completion or adding events',()=>{
  const ui=app();
  ui.context.course={kind:'course',status:'pending',attendance:{status:'done'}};
  assert.match(ui.run('statusBadge(course)'),/已签到/);
  assert.equal(ui.run('isCompleted(course)'),false);
  assert.equal(ui.run('attendanceLabel({kind:"course",status:"pending"})'),"");
});

function app() {
  const elements = new Map();
  const node = (selector) => {
    if (!elements.has(selector)) elements.set(selector, { innerHTML: '', textContent: '', value: '', dataset: {}, scrollTop: 0, scrollLeft: 0, clientHeight: 600,
      classList: { toggle() {}, add() {}, remove() {} } });
    return elements.get(selector);
  };
  const context = vm.createContext({ Intl, Date, Set, Map, console, setTimeout, clearTimeout,
    document: { querySelector: node, querySelectorAll: () => [], addEventListener() {} } });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../web/boya.js'), 'utf8'), context);
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8'), context);
  return { context, elements, run: (code) => vm.runInContext(code, context) };
}

test('shared details preserve zero scores and expose safe collapsed long content', () => {
  const ui = app();
  ui.context.event = {id:1,kind:'assignment',source:'spoc',title:'任务',due_at:'2030-09-01T23:59:00',status:'done',metadata:{
    earned_score:0,max_score:100,graded:true,grade_comment:'<img src=x onerror=bad()>\n评语',
    description:'长正文'.repeat(12000)+'<script>bad()</script>',submitted_at:'2030-08-31T10:00:00'}};
  const html = ui.run('renderEventDetail(event)');
  assert.match(html, /<dt>得分<\/dt><dd>0<\/dd>/);
  assert.match(html, /<dt>满分<\/dt><dd>100<\/dd>/);
  assert.match(html, /教师已批阅|2030/);
  assert.match(html, /<summary>教师评语<\/summary>/);
  assert.match(html, /<summary>作业正文<\/summary>/);
  assert.match(html, /&lt;img|&lt;script/);
  assert.ok(html.includes('长正文'.repeat(12000)));
  assert.doesNotMatch(html, /<details[^>]*\bopen\b|<img\b|<script\b/);
});

test('unknown personal grade is not replaced by assignment maximum or legacy score', () => {
  const ui = app();
  const html = ui.run('renderEventDetail({kind:"assignment",source:"spoc",status:"pending",metadata:{max_score:100,score:100,grading_known:false}})');
  assert.doesNotMatch(html, /<dt>得分/);
  assert.match(html, /<dt>批阅<\/dt><dd>未知/);
  assert.doesNotMatch(html, /event-disclosure/);
});

test('JUDGE problem and course sections only show known fields and default closed', () => {
  const ui = app();
  let html = ui.run('renderEventDetail({kind:"assignment",source:"judge",metadata:{problems:[{title:"零分题",earned_score:0,max_score:10,submission_status:"submitted"},{title:"未知题"}],description:"整页文字"}})');
  assert.match(html, /题目明细（2）/);
  assert.match(html, /<td>已提交<\/td><td>0<\/td><td>10/);
  assert.match(html, /<td>未知<\/td><td>—<\/td><td>—/);
  assert.match(html, /作业页面内容/);
  html = ui.run('renderEventDetail({kind:"course",source:"byxt",location:"教室",attendance:{status:"done",checked_at:"2030-09-01T09:00:00"},metadata:{teacher:"教师",courseCode:"code",beginSection:3,week_number:0}})');
  assert.match(html, /课程信息|已签到|教室|教师/);
  assert.match(html, /<dt>教学周<\/dt><dd>0/);
  assert.doesNotMatch(html, /<dt>结束节次|<details[^>]*\bopen\b/);
});

test('ignored and completed events still have a read-only details entry', () => {
  const ui = app();
  ui.run('state.data.events=[{id:7,kind:"assignment",source:"judge",title:"已忽略条目",ignored:true,status:"done"}]; $("#calendar-detail-dialog").showModal=()=>{}; openCalendarDetail("7");');
  assert.equal(ui.elements.get('#calendar-detail-title').textContent, '已忽略条目');
  assert.match(ui.elements.get('#calendar-detail-body').innerHTML, /已忽略/);
  ui.run('state.taskStatus="ignored";renderTasks();');
  assert.match(ui.elements.get('#task-list').innerHTML, /data-calendar-event="7"/);
  assert.match(ui.elements.get('#task-list').innerHTML, /data-ignore-event="7"/);
});

test('short weekly cards keep title and time before secondary content', () => {
  const ui = app();
  ui.run('state.cursor=new Date("2030-09-02T12:00:00");state.data.events=[{id:1,kind:"course",source:"byxt",title:"短课程",starts_at:"2030-09-02T14:50:00",ends_at:"2030-09-02T15:35:00",location:"教室",metadata:{teacher:"教师"}}];');
  const html = ui.run('renderWeek()');
  assert.match(html, /calendar-event compact/);
  assert.match(html, /<strong>短课程<\/strong><span class="calendar-event-time">14:50–15:35/);
  const css = fs.readFileSync(path.join(__dirname,'../web/styles.css'),'utf8');
  assert.match(css, /\.calendar-event > \* \{ flex-shrink: 0/);
  assert.match(css, /\.calendar-event.compact \.calendar-event-meta[^}]+display: none/);
});

test('task filters hide expired items, show year and submitted labels', () => {
  const ui = app();
  ui.run(`state.data.events = [
    { title:'old', source:'judge', kind:'assignment', due_at:'2024-01-01', status:'pending', completion_state:'unsubmitted' },
    { title:'pending', source:'spoc', kind:'assignment', due_at:'2030-09-01', status:'pending', completion_state:'unsubmitted' },
    { title:'submitted', source:'spoc', kind:'assignment', due_at:'2030-09-02', status:'done', completion_state:'submitted', status_label:'已提交' },
    { title:'partial', source:'judge', kind:'assignment', due_at:'2030-09-03', status:'partial', completion_state:'partial', metadata:{submitted_count:1,total_problems:2} }
  ]; renderTasks();`);
  let html = ui.elements.get('#task-list').innerHTML;
  assert.match(html, /2030/); assert.match(html, /pending/); assert.match(html, /部分提交 1\/2/);
  assert.doesNotMatch(html, />old</); assert.doesNotMatch(html, />submitted</);
  ui.run('state.taskStatus = "submitted"; renderTasks();');
  html = ui.elements.get('#task-list').innerHTML;
  assert.match(html, /已提交/); assert.doesNotMatch(html, />pending</);
  ui.run('state.taskStatus = "any"; state.taskHistory = true; renderTasks();');
  assert.match(ui.elements.get('#task-list').innerHTML, /2024/);
});

test('week covers midnight through 24:00 and keeps deadlines outside duration grid', () => {
  const ui = app();
  ui.run(`state.cursor = new Date(2030, 8, 2); state.data.events = [
    {id:1,title:'midnight',kind:'signin',starts_at:new Date(2030,8,2,0,15).toISOString(),ends_at:new Date(2030,8,2,1).toISOString(),status:'missing'},
    {id:2,title:'late',kind:'signin',starts_at:new Date(2030,8,2,23,59).toISOString(),status:'missing'},
    {id:3,title:'deadline',kind:'assignment',due_at:new Date(2030,8,2,23).toISOString(),status:'pending'}
  ];`);
  const html = ui.run('renderWeek()');
  assert.match(html, /00:00/); assert.match(html, /24:00/); assert.match(html, /23:00/);
  assert.match(html, /class="week-deadline" data-calendar-event="3"/);
  assert.doesNotMatch(html, /class="calendar-event" data-calendar-event="3"/);
  assert.equal(ui.run('weekDayLayout(startOfWeek(state.cursor)).length'), 2);
  assert.equal(ui.run('weekDayLayout(startOfWeek(state.cursor)).every(item => item.top >= 0 && item.bottom <= 24 * WEEK_HOUR_HEIGHT)'), true);
});

test('week splits cross-midnight entries and paints overlapping entries in lanes', () => {
  const ui = app();
  ui.run(`state.cursor = new Date(2030,8,2); state.data.events = [
    {id:1,kind:'signin',starts_at:new Date(2030,8,2,23).toISOString(),ends_at:new Date(2030,8,3,2).toISOString()},
    {id:2,kind:'signin',starts_at:new Date(2030,8,2,23,30).toISOString(),ends_at:new Date(2030,8,3,1).toISOString()}
  ];`);
  assert.equal(ui.run('weekDayLayout(state.cursor).length'), 2);
  assert.equal(ui.run('weekDayLayout(state.cursor).every(item => item.lanes === 2)'), true);
  assert.equal(ui.run('weekDayLayout(addDays(state.cursor,1)).every(item => item.top === 0 && item.continued)'), true);
});

test('week preserves scroll across refresh, including midnight, without resetting on hidden page', () => {
  const ui = app();
  ui.run('renderCalendar();');
  assert.equal(ui.elements.get('#calendar-canvas').scrollTop, 8 * 56);
  ui.run('state.page = "calendar"; $("#calendar-canvas").scrollTop = 0; renderCalendar();');
  assert.equal(ui.elements.get('#calendar-canvas').scrollTop, 0);
  ui.run('state.weekScrollTop = 18 * 56; state.page = "tasks"; $("#calendar-canvas").scrollTop = 0; renderCalendar();');
  assert.equal(ui.elements.get('#calendar-canvas').scrollTop, 18 * 56);
});

test('course reminder supports start or end timing, while assignments keep submission filters', () => {
  const ui = app();
  for (const profile of ['spoc-assignment', 'judge-assignment']) {
    ui.run(`renderProfileSettings('${profile}');`);
    const html = ui.elements.get('#profile-settings').innerHTML;
    assert.match(html, /completion_filter/);
    assert.doesNotMatch(html, /data-profile-setting="kind"/);
    assert.match(html, /data-advance="days"/);
    assert.match(html, /data-advance="hours"/); assert.match(html, /data-advance="minutes"/);
    assert.match(html, /添加提醒时间/); assert.doesNotMatch(html, /再次提醒|作业类型/);
  }
  ui.run("renderProfileSettings('course');");
  const html = ui.elements.get('#profile-settings').innerHTML;
  assert.match(html, /before_start/); assert.match(html, /before_end/); assert.match(html, /advance_minutes/);
  assert.doesNotMatch(html, /completion_filter/);
  assert.equal(ui.run('activeProfiles.has("signin")'), true);
  assert.equal(ui.run('activeProfiles.has("course")'), true);
});

test('course timing preserves old rules and uses the selected course end anchor', () => {
  const ui = app();
  assert.equal(ui.run('profileSentence({profile:"course",trigger:{anchor:"starts_at",offset_minutes:-15},profile_settings:{before_start:15}})'), '上课前 15 分钟提醒');
  assert.equal(ui.run('profileSentence({profile:"course",trigger:{anchor:"ends_at",offset_minutes:-20},profile_settings:{timing:"before_end",advance_minutes:20}})'), '课程结束前 20 分钟提醒');
  ui.run('renderProfileSettings("course",{timing:"before_end",advance_minutes:20});');
  assert.match(ui.elements.get('#profile-settings').innerHTML, /value="before_end" selected/);
});

test('individual sync controls expose progress and leave other sources available', () => {
  const ui = app();
  assert.match(ui.run('renderSourceRow({id:"spoc", label:"SPOC", enabled:true})'), /data-source-sync="spoc"/);
  const syncing = ui.run('renderSourceRow({id:"judge", label:"JUDGE", enabled:true, syncing:true, sync_progress:{completed:3,total:13,phase:"读取数据"}})');
  assert.match(syncing, /3\/13/); assert.match(syncing, /disabled/);
  const parked = ui.run('renderSourceRow({id:"iclass", label:"课程签到", available:false})');
  assert.match(parked, /Coming Soon/); assert.doesNotMatch(parked, /data-source-sync/);
});

test('each connection exposes only its own second-level rule types', () => {
  const ui = app();
  ui.run('renderRuleTypes("byxt");');
  let html = ui.elements.get('#rule-profile').innerHTML;
  assert.match(html, /课程变更提醒/); assert.match(html, /课程提醒/);
  assert.doesNotMatch(html, /作业|评分/);
  assert.equal(ui.run('connectionRuleTypes.byxt.length'), 2);
  ui.run('renderRuleTypes("spoc");');
  html = ui.elements.get('#rule-profile').innerHTML;
  assert.match(html, /作业新评分提醒/); assert.match(html, /新作业提醒/);
  assert.equal(ui.run('connectionRuleTypes.spoc.length'), 3);
  ui.run('renderRuleTypes("judge");');
  html = ui.elements.get('#rule-profile').innerHTML;
  assert.doesNotMatch(html, /评分|课程/);
  assert.equal(ui.run('connectionRuleTypes.judge.length'), 2);
});

test('advance time sums days hours minutes and rejects at-deadline and invalid inputs', () => {
  const ui = app();
  assert.equal(ui.run('advanceMinutes({days:0,hours:2,minutes:30})'), 150);
  assert.equal(ui.run('advanceMinutes({days:1,hours:1,minutes:5})'), 1505);
  for (const point of ['{days:0,hours:0,minutes:0}', '{minutes:-1}', '{hours:24}', '{minutes:1.5}']) {
    assert.throws(() => ui.run(`advanceMinutes(${point})`));
  }
  assert.equal(ui.run('advanceText({days:0,hours:2,minutes:30})'), '2 小时 30 分钟');
});

test('legacy assignment rules load actual effective timings rather than default guesses', () => {
  const ui = app();
  const points = ui.run('deadlinePoints({trigger:{anchor:"due_at",offset_minutes:-1440},actions:[{after_minutes:0},{after_minutes:1320}]})');
  assert.equal(JSON.stringify(points), JSON.stringify([{days:1,hours:0,minutes:0},{days:0,hours:2,minutes:0}]));
  assert.equal(ui.run('profileSentence({profile:"spoc-assignment",profile_settings:{reminder_times:[{days:0,hours:2,minutes:30}]}})'), '截止前 2 小时 30 分钟提醒');
});

test('course change contains additions and grade settings never hide submitted grades', () => {
  const ui = app();
  ui.run('renderProfileSettings("course-change");');
  const html = ui.elements.get('#profile-settings').innerHTML;
  assert.match(html, /新增课程|课程取消/); assert.match(html, /data-change-type="location"/);
  assert.doesNotMatch(html, /reminder-time-row|completion_filter/);
  ui.run('renderProfileSettings("spoc-assignment-grade");');
  assert.doesNotMatch(ui.elements.get('#profile-settings').innerHTML, /completion_filter|作业类型/);
  assert.doesNotMatch(html, /首次完整同步|连接页面可设置|每.*分钟检查|form-hint/);
  assert.equal(ui.elements.get('#rule-settings-fieldset').hidden, true);
});

test('course tasks show course status instead of attendance or submission', () => {
  const ui = app();
  ui.run('state.data.preferences.task_visible_kinds=["assignment","course","boya"];');
  ui.run('state.data.events = [{id:1,kind:"course",source:"byxt",title:"真实课表课程",starts_at:"2030-09-01",ends_at:"2030-09-02",status:"upcoming",completion_state:"not_applicable",status_label:"待上课",location:"教室",metadata:{teacher:"教师"}}]; renderTasks();');
  const html = ui.elements.get('#task-list').innerHTML;
  assert.match(html, /真实课表课程/); assert.match(html, /待上课/); assert.doesNotMatch(html, /签到|未提交/);
  ui.run('state.taskStatus="submitted"; renderTasks();');
  assert.doesNotMatch(ui.elements.get('#task-list').innerHTML, /真实课表课程/);
});

test('personal task visibility defaults to assignments and leaves calendars untouched', () => {
  const ui = app();
  ui.run(`state.data.events = [
    {id:1,kind:"course",source:"byxt",title:"teaching",starts_at:"2030-09-01",ends_at:"2030-09-02"},
    {id:2,kind:"boya",source:"bykc",title:"boya",starts_at:"2030-09-01",ends_at:"2030-09-02"},
    {id:3,kind:"assignment",source:"spoc",title:"spoc-work",due_at:"2030-09-02"},
    {id:4,kind:"assignment",source:"judge",title:"judge-work",due_at:"2030-09-02"}
  ]; renderTasks();`);
  let html=ui.elements.get('#task-list').innerHTML;
  assert.match(html,/spoc-work|judge-work/); assert.doesNotMatch(html,/teaching|data-calendar-event="2"/);
  assert.equal(ui.run('visibleCalendarEvents().length'),4);
  ui.run('state.data.preferences={task_visible_kinds:["assignment","boya"],task_visible_sources:["judge","bykc"]}; renderTasks();');
  html=ui.elements.get('#task-list').innerHTML;
  assert.match(html,/judge-work/); assert.match(html,/data-calendar-event="2"/); assert.doesNotMatch(html,/spoc-work|teaching/);
  ui.run('state.data.preferences.task_visible_kinds=[];renderTasks();');
  assert.doesNotMatch(ui.elements.get('#task-list').innerHTML,/data-calendar-event/);
  assert.equal(ui.run('visibleCalendarEvents().length'),4);
});

test('Boya uses its own seven rule types and minute-level filtered catalog editor', () => {
  const ui=app();
  const types=JSON.parse(ui.run('JSON.stringify(connectionRuleTypes.bykc)'));
  assert.equal(types.length,7); assert.ok(types.some(([id])=>id==='boya-seats'));
  assert.ok(types.some(([id])=>id==='boya-new-course'));
  assert.doesNotMatch(ui.run('renderBoyaRuleSettings("boya-enrollment-soon",profileDefaults("boya-enrollment-soon"))'),/再次提醒|同步仅建立基线/);
  ui.run('state.data.boya_options={categories:[{value:"2",label:"美育"}],campuses:[{value:"1",label:"学院路"}],sign_types:[{value:"2",label:"签到＋签退"}]};');
  const html=ui.run('renderBoyaRuleSettings("boya-enrollment-soon",{categories:["2"],sign_types:["2"],advance_minutes:[11,1]})');
  assert.match(html,/课程分类|适用校区|签到方式|报名开放前/);
  assert.match(html,/data-boya-filter="categories"[^>]+checked/);
  assert.match(html,/data-advance="minutes"/); assert.match(html,/添加时间/);
  assert.match(ui.run('renderBoyaRuleSettings("boya-seats",profileDefaults("boya-seats"))'),/最低余量|通知间隔/);
});

test('Boya details keep long text collapsed and zero personal scores visible',()=>{
  const ui=app();
  const html=ui.run('renderEventDetail({kind:"boya",source:"bykc",title:"博雅",metadata:{score:0,sign_type:"2",category_names:["美育"],campus_names:["学院路"],description:"<script>bad()</script>",homework_description:"要求",sign_windows:{signStartDate:"2030-09-01T10:00:00+08:00"}}})');
  assert.match(html,/<dt>得分<\/dt><dd>0<\/dd>/);
  assert.match(html,/签到＋签退|签到开始|作业信息|详细说明/);
  assert.match(html,/&lt;script/); assert.doesNotMatch(html,/<script|<details[^>]*\bopen\b/);
});

test('multiline inbox only shows the first line until expanded',()=>{
  const ui=app();
  ui.run('state.data.inbox=[{title:"提醒",body:"作业截止提醒\\n地点：教室\\n长正文",created_at:"2030-09-01"}]; renderInbox();');
  const html=ui.elements.get('#inbox-list').innerHTML;
  assert.match(html,/<summary>作业截止提醒<\/summary>/);
  assert.doesNotMatch(html,/<details[^>]*\bopen\b/);
});

test('calendar completion visibility is independent of task filters', () => {
  const ui = app();
  ui.run('state.data.events = [{status:"pending"},{status:"done"}];');
  assert.equal(ui.run('visibleCalendarEvents().length'), 1);
  ui.run('state.showCompleted = true;');
  assert.equal(ui.run('visibleCalendarEvents().length'), 2);
});

test('ignored items expose undo, and never appear in calendar or active tasks', () => {
  const ui = app();
  ui.run('state.data.events = [{id:1,title:"ignored-task",kind:"assignment",source:"judge",status:"pending",ignored:true,due_at:"2030-09-01"}]; renderTasks();');
  assert.doesNotMatch(ui.elements.get('#task-list').innerHTML, /ignored-task/);
  ui.run('state.taskStatus = "ignored"; renderTasks();');
  const html = ui.elements.get('#task-list').innerHTML;
  assert.match(html, /已忽略/); assert.match(html, /取消忽略/); assert.match(html, /data-ignore-event="1"/);
  assert.equal(ui.run('visibleCalendarEvents().length'), 0);
});
