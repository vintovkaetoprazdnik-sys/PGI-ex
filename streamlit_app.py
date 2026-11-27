import streamlit as st
import pandas as pd
import datetime
import gspread
from google.oauth2.service_account import Credentials
from ortools.sat.python import cp_model
import calendar
import json
import os
import io

# --- НАСТРОЙКИ СТРАНИЦЫ ---
st.set_page_config(page_title="ПГИ Экскурсии", page_icon="🗓️", layout="wide")

# --- ЗАГРУЗКА КОНФИГА ---
CONFIG_FILE = "config.json"
default_config = {
    "rest_time_hours": 1.5,
    "presence_bonus": 1000,
    "quantity_bonus": 10,
    "start_time_hour": 11,
    "end_time_hour": 18,
    "time_step_minutes": 30,
    "duplicate_penalty": 200, # Новый штраф за дубли
    "spread_penalty": 100     # Новый штраф за близость
}
if os.path.exists(CONFIG_FILE):
    with open(CONFIG_FILE, "r") as f:
        current_config = json.load(f)
        # Добавляем недостающие ключи, если конфиг старый
        for k, v in default_config.items():
            if k not in current_config:
                current_config[k] = v
else:
    current_config = default_config.copy()

# --- ПОДКЛЮЧЕНИЕ К GOOGLE SHEETS ---
creds_dict = st.secrets["gcp_service_account"]
creds = Credentials.from_service_account_info(
    creds_dict,
    scopes=["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
)
client = gspread.authorize(creds)
SHEET_ID = '1CVv12SLURD2-he_TEwI9LTIYbe22-KN4yth67Hngyno'

@st.cache_data(ttl=10)
def load_data():
    spreadsheet = client.open_by_key(SHEET_ID)
    df_tours = pd.DataFrame(spreadsheet.worksheet('ПГИ Экскурсии').get_all_records())
    df_avail = pd.DataFrame(spreadsheet.worksheet('Доступность').get_all_records())
    try:
        ws_guides = spreadsheet.worksheet('Гиды')
        df_guides = pd.DataFrame(ws_guides.get_all_records())
    except gspread.exceptions.WorksheetNotFound:
        df_guides = pd.DataFrame(columns=["Имя гида"])
    return df_tours, df_avail, df_guides

# --- ФУНКЦИЯ ДЛЯ АЛГОРИТМА ---
def generate_schedule(df_tours, df_avail, year, month, weekends_only, config):
    DAYS_IN_MONTH = calendar.monthrange(year, month)[1]
    REST_TIME_HOURS = config["rest_time_hours"]
    PRESENCE_BONUS = config["presence_bonus"]
    QUANTITY_BONUS = config["quantity_bonus"]
    DUPLICATE_PENALTY = config["duplicate_penalty"]
    SPREAD_PENALTY = config["spread_penalty"]
    
    possible_start_times = []
    curr = config["start_time_hour"]
    step = config["time_step_minutes"] / 60.0
    while curr <= config["end_time_hour"]:
        possible_start_times.append(curr)
        curr += step

    days_info = {day: datetime.date(year, month, day).weekday() for day in range(1, DAYS_IN_MONTH + 1)}
    valid_days = [d for d in range(1, DAYS_IN_MONTH + 1) if not weekends_only or days_info[d] >= 5]
    total_valid_days = len(valid_days)

    all_guides = set()
    for guides_str in df_tours['Гиды'].dropna():
        for g in guides_str.split(';'):
            if g.strip(): all_guides.add(g.strip())
    for g in df_avail['Гид'].dropna():
        all_guides.add(str(g).strip())

    guide_limits = {g: {} for g in all_guides}
    for index, row in df_avail.iterrows():
        guide = str(row['Гид']).strip()
        date_str = str(row['Дата']).strip()
        is_available = str(row['Доступен']).strip().lower() == 'да'
        if not guide or not date_str: continue
        try:
            dt = datetime.datetime.strptime(date_str, '%d.%m.%Y')
            if dt.month == month and dt.year == year:
                day = dt.day
                if is_available:
                    time_from = str(row['Время с']).strip() if pd.notna(row['Время с']) else '00:00'
                    time_to = str(row['Время до']).strip() if pd.notna(row['Время до']) else '23:59'
                    h_from = int(time_from.split(':')[0]) + int(time_from.split(':')[1])/60
                    h_to = int(time_to.split(':')[0]) + int(time_to.split(':')[1])/60
                    guide_limits[guide][day] = (h_from, h_to)
        except ValueError:
            pass

    tours = []
    for index, row in df_tours.iterrows():
        name = str(row['Название экскурсии']).strip()
        if not name: continue
        guides = [g.strip() for g in str(row['Гиды']).split(';') if g.strip()]
        is_summer = month in [6, 7, 8]
        freq = int(row['Лето, раз в месяц']) if is_summer else int(row['Зима, раз в месяц'])
        days_limit = None
        days_str = str(row['Ограничение по дням']).strip().lower()
        if 'суббота' in days_str: days_limit = [5]
        elif 'воскресенье' in days_str: days_limit = [6]
        time_val = row['Ограничение по времени']
        fixed_time = None
        if pd.notna(time_val) and str(time_val).strip():
            t_str = str(time_val).strip()
            fixed_time = int(t_str.split(':')[0]) + int(t_str.split(':')[1])/60 if ':' in t_str else float(t_str)
        duration = float(row['Продолжительность'])
        tours.append({"name": name, "duration": duration, "guides": guides, "days": days_limit, "time": fixed_time, "freq": freq, "t_idx": len(tours)})

    model = cp_model.CpModel()
    assignments = [] 
    intervals = {g: [] for g in all_guides} 

    for tour in tours:
        t_idx = tour["t_idx"]
        tour_assigns = {}
        for day in range(1, DAYS_IN_MONTH + 1):
            day_of_week = days_info[day]
            if weekends_only and day_of_week < 5: continue 
            if tour["days"] is not None and day_of_week not in tour["days"]: continue
                
            for g_idx, guide_name in enumerate(tour["guides"]):
                if day not in guide_limits.get(guide_name, {}): continue
                g_start_limit, g_end_limit = guide_limits[guide_name][day]
                times_to_check = [tour["time"]] if tour["time"] is not None else possible_start_times
                
                for start_time in times_to_check:
                    if start_time < g_start_limit or (start_time + tour["duration"]) > g_end_limit: continue
                    var = model.NewBoolVar(f't_{t_idx}_d_{day}_g_{guide_name}_time_{start_time}')
                    tour_assigns[(day, guide_name, start_time)] = var
                    start_min = int(start_time * 60)
                    end_min = int((start_time + tour["duration"] + REST_TIME_HOURS) * 60) 
                    intervals[guide_name].append(model.NewOptionalIntervalVar(
                        model.NewConstant(start_min), model.NewConstant(end_min - start_min), 
                        model.NewConstant(end_min), var, f'opt_int_{t_idx}_{day}_{guide_name}_t_{start_time}'
                    ))
        assignments.append(tour_assigns)

    # 1. Гид не может вести 2 экскурсии одновременно (жесткое)
    for guide_name, guide_intervals in intervals.items():
        if guide_intervals: model.AddNoOverlap(guide_intervals)

    # 2. Не превышаем заданную частоту (жесткое)
    total_assigned_vars = []
    for tour in tours:
        t_idx = tour["t_idx"]
        vars_for_tour = list(assignments[t_idx].values())
        if vars_for_tour:
            model.Add(sum(vars_for_tour) <= tour["freq"])
            total_assigned_vars.extend(vars_for_tour)

    # 3. В один день и в одно время на одну экскурсию идет только один гид (жесткое)
    for tour in tours:
        t_idx = tour["t_idx"]
        slots = set((day, start_time) for (day, g, start_time) in assignments[t_idx].keys())
        for day, start_time in slots:
            vars_for_slot = [var for (d, g, st), var in assignments[t_idx].items() if d == day and st == start_time]
            model.Add(sum(vars_for_slot) <= 1)

    # --- МЯГКИЕ ОГРАНИЧЕНИЯ (ШТРАФЫ) ---
    
    # 4. Штраф за дубли (две одинаковые экскурсии у одного гида в один день)
    duplicate_penalties = []
    for tour in tours:
        t_idx = tour["t_idx"]
        for guide_name in tour["guides"]:
            for day in range(1, DAYS_IN_MONTH + 1):
                vars_for_day_guide = [var for (d, g, st), var in assignments[t_idx].items() if d == day and g == guide_name]
                # Если гид ведет эту экскурсию больше 1 раза в день, создаем штраф
                if len(vars_for_day_guide) > 1:
                    # Количество экскурсий в день минус 1 (это и есть "лишние" дубли)
                    excess = model.NewIntVar(0, len(vars_for_day_guide), f'excess_{t_idx}_{guide_name}_{day}')
                    model.Add(excess >= sum(vars_for_day_guide) - 1)
                    duplicate_penalties.append(excess * DUPLICATE_PENALTY)

    # 5. Штраф за близость (равномерность)
    spread_penalties = []
    for tour in tours:
        if tour["freq"] > 1 and total_valid_days > 0:
            t_idx = tour["t_idx"]
            ideal_gap = total_valid_days / tour["freq"]
            days_available = sorted(list(set(d for (d, g, st) in assignments[t_idx].keys())))
            
            for i in range(len(days_available)):
                for j in range(i + 1, len(days_available)):
                    d1, d2 = days_available[i], days_available[j]
                    gap = d2 - d1
                    # Если разрыв меньше идеального, добавляем штраф
                    if gap < ideal_gap * 0.8: 
                        vars_d1 = [var for (d, g, st), var in assignments[t_idx].items() if d == d1]
                        vars_d2 = [var for (d, g, st), var in assignments[t_idx].items() if d == d2]
                        
                        if vars_d1 and vars_d2:
                            both_selected = model.NewBoolVar(f'both_{t_idx}_{d1}_{d2}')
                            model.Add(sum(vars_d1) >= 1).OnlyEnforceIf(both_selected)
                            model.Add(sum(vars_d2) >= 1).OnlyEnforceIf(both_selected)
                            spread_penalties.append(both_selected * SPREAD_PENALTY)

    # 6. Приоритет "Хотя бы 1 раз"
    tour_is_present = []
    bonus_for_presence = []
    for tour in tours:
        t_idx = tour["t_idx"]
        vars_for_tour = list(assignments[t_idx].values())
        if vars_for_tour:
            is_present = model.NewBoolVar(f'is_present_{t_idx}')
            model.Add(sum(vars_for_tour) >= 1).OnlyEnforceIf(is_present)
            model.Add(sum(vars_for_tour) == 0).OnlyEnforceIf(is_present.Not())
            tour_is_present.append(is_present)
            bonus_for_presence.append(is_present * PRESENCE_BONUS)
        else:
            tour_is_present.append(model.NewConstant(0))
            bonus_for_presence.append(model.NewConstant(0))

    # ИТОГОВАЯ ФУНКЦИЯ
    model.Maximize(
        sum(total_assigned_vars) * QUANTITY_BONUS + 
        sum(bonus_for_presence) - 
        sum(duplicate_penalties) - 
        sum(spread_penalties)
    )

    class SolutionPrinter(cp_model.CpSolverSolutionCallback):
        def __init__(self, assignments, tours, model):
            cp_model.CpSolverSolutionCallback.__init__(self)
            self._assignments = assignments
            self._tours = tours
            self._model = model
            self.solutions = []

        def on_solution_callback(self):
            current_solution = []
            current_solution_vars = [] 
            for t_idx, tour_assigns in enumerate(self._assignments):
                for (day, guide_name, start_time), var in tour_assigns.items():
                    if self.Value(var) == 1:
                        current_solution_vars.append(var)
                        start_str = f"{int(start_time):02d}:{int((start_time % 1) * 60):02d}"
                        end_time = start_time + self._tours[t_idx]["duration"]
                        end_str = f"{int(end_time):02d}:{int((end_time % 1) * 60):02d}"
                        current_solution.append({
                            "Дата": f"{day:02d}.{month:02d}.{year}",
                            "День недели": ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"][days_info[day]],
                            "Экскурсия": self._tours[t_idx]["name"],
                            "Гид": guide_name,
                            "Время": f"{start_str} - {end_str}"
                        })
            if current_solution:
                self.solutions.append(current_solution)
                if len(self.solutions) >= 3: 
                    self.StopSearch()
                else:
                    self._model.Add(sum(current_solution_vars) <= len(current_solution_vars) - 1)

    solver = cp_model.CpSolver()
    solver.parameters.enumerate_all_solutions = True
    solver.parameters.max_time_in_seconds = 20.0

    printer = SolutionPrinter(assignments, tours, model)
    solver.Solve(model, printer)
    return printer.solutions

# --- ФУНКЦИЯ ДЛЯ ГЕНЕРАЦИИ EXCEL-МАТРИЦЫ ---
def create_excel_matrix(df_result, all_guides_list, month, year):
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df_result.to_excel(writer, sheet_name='Список', index=False)
        
        dates = sorted(list(set(df_result['Дата'])))
        matrix_data = []
        for guide in all_guides_list:
            row = {"Гиды": guide}
            for date in dates:
                row[date] = ""
            matrix_data.append(row)
            
        df_matrix = pd.DataFrame(matrix_data)
        
        for idx, row in df_result.iterrows():
            guide = row['Гид']
            date = row['Дата']
            time_str = row['Время'].split(' - ')[0]
            tour = row['Экскурсия']
            
            if date in df_matrix.columns:
                current_val = df_matrix.loc[df_matrix['Гиды'] == guide, date].values[0]
                new_val = f"{time_str} {tour}"
                if current_val:
                    df_matrix.loc[df_matrix['Гиды'] == guide, date] = f"{current_val}\n{new_val}"
                else:
                    df_matrix.loc[df_matrix['Гиды'] == guide, date] = new_val
            
        df_matrix.to_excel(writer, sheet_name='Матрица', index=False)
        
        worksheet = writer.sheets['Матрица']
        from openpyxl.styles import Alignment
        for col in worksheet.columns:
            for cell in col:
                cell.alignment = Alignment(vertical='center', wrap_text=True)
        
        worksheet.column_dimensions['A'].width = 25
        for col_letter in ['B', 'C', 'D', 'E', 'F', 'G', 'H', 'I', 'J', 'K', 'L', 'M', 'N', 'O', 'P']:
            worksheet.column_dimensions[col_letter].width = 20
    return output.getvalue()

# --- ИНТЕРФЕЙС ---
st.title("🗓️ Планировщик экскурсий ПГИ")

try:
    df_tours, df_avail, df_guides = load_data()
except Exception as e:
    st.error(f"❌ Ошибка при чтении таблицы: {e}")
    st.stop()

if 'schedule_results' not in st.session_state:
    st.session_state['schedule_results'] = None
if 'schedule_month_name' not in st.session_state:
    st.session_state['schedule_month_name'] = ""
if 'all_guides_list' not in st.session_state:
    st.session_state['all_guides_list'] = []

tab1, tab2, tab3 = st.tabs(["Генератор расписания", "Управление доступностью гидов", "⚙️ Настройки алгоритма"])

# --- ВКЛАДКА 1: ГЕНЕРАТОР ---
with tab1:
    st.markdown("Приложение автоматически считывает данные из вашей [Google Таблицы](https://docs.google.com/spreadsheets/d/1CVv12SLURD2-he_TEwI9LTIYbe22-KN4yth67Hngyno).")
    
    col1, col2, col3 = st.columns([2, 1, 1])
    with col1:
        month_names = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь", "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]
        selected_month_name = st.selectbox("Выберите месяц для планирования:", month_names, index=11)
        selected_month = month_names.index(selected_month_name) + 1
    with col2:
        selected_year = st.number_input("Год:", min_value=2024, max_value=2030, value=2026)
    with col3:
        st.write("")
        st.write("")
        weekends_only = st.checkbox("Только выходные", value=True)

    if st.button("🚀 Сгенерировать расписание", type="primary"):
        with st.spinner("Алгоритм подбирает лучшие варианты..."):
            solutions = generate_schedule(df_tours, df_avail, selected_year, selected_month, weekends_only, current_config)
            
            if not solutions:
                st.warning("⚠️ Не удалось составить расписание. Проверьте доступность гидов.")
                st.session_state['schedule_results'] = None
            else:
                st.session_state['schedule_results'] = solutions
                st.session_state['schedule_month_name'] = selected_month_name
                all_guides_list = sorted(list(set(df_avail['Гид'].dropna().astype(str).str.strip().tolist()) | set([g.strip() for gs in df_tours['Гиды'].dropna() for g in gs.split(';') if g.strip()])))
                st.session_state['all_guides_list'] = all_guides_list

    if st.session_state['schedule_results']:
        solutions = st.session_state['schedule_results']
        st.success(f"✅ Найдено {len(solutions)} вариантов расписания!")
        
        for i, sol in enumerate(solutions):
            st.subheader(f"Вариант {i+1}")
            df_result = pd.DataFrame(sol).sort_values(by=["Дата", "Время"])
            st.dataframe(df_result, use_container_width=True, hide_index=True)
            
            excel_data = create_excel_matrix(df_result, st.session_state['all_guides_list'], selected_month, selected_year)
            
            st.download_button(
                label="📥 Скачать в Excel (Матрица)",
                data=excel_data,
                file_name=f"schedule_{st.session_state['schedule_month_name']}_v{i+1}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                key=f"download_{i}"
            )

# --- ВКЛАДКА 2: ВВОД ДОСТУПНОСТИ ---
with tab2:
    st.header("Управление доступностью гидов")
    with st.expander("➕ Добавить нового гида в базу"):
        new_guide_name = st.text_input("Введите фамилию и имя нового гида:")
        if st.button("Добавить гида"):
            if new_guide_name.strip():
                try:
                    spreadsheet = client.open_by_key(SHEET_ID)
                    ws_guides = spreadsheet.worksheet('Гиды')
                    ws_guides.append_row([new_guide_name.strip()])
                    st.success(f"Гид {new_guide_name} добавлен!")
                    st.cache_data.clear()
                    st.rerun()
                except Exception as e:
                    st.error(f"Ошибка: {e}")
            else:
                st.warning("Введите имя.")

    st.markdown("---")
    all_guides_list = set(df_guides['Имя гида'].dropna().astype(str).str.strip().tolist())
    for guides in df_tours['Гиды'].dropna():
        for g in guides.split(';'):
            if g.strip(): all_guides_list.add(g.strip())
    for g in df_avail['Гид'].dropna():
        all_guides_list.add(str(g).strip())
    all_guides_list = sorted(list(all_guides_list))
    
    if not all_guides_list:
        st.warning("В базе пока нет гидов. Добавьте первого выше.")
        st.stop()
        
    selected_guide = st.selectbox("Выберите гида для редактирования:", all_guides_list)
    
    st.markdown("**Выберите месяц, на который вносите данные:**")
    col_m, col_y = st.columns(2)
    with col_m:
        tab2_month_name = st.selectbox("Месяц:", month_names, index=11, key="tab2_month")
        tab2_month = month_names.index(tab2_month_name) + 1
    with col_y:
        tab2_year = st.number_input("Год:", min_value=2024, max_value=2030, value=2026, key="tab2_year")
    
    df_guide_current = df_avail[df_avail['Гид'].astype(str).str.strip() == selected_guide].copy()
    if not df_guide_current.empty:
        def check_date_match(date_str):
            try:
                dt = datetime.datetime.strptime(str(date_str).strip(), '%d.%m.%Y')
                return dt.month == tab2_month and dt.year == tab2_year
            except:
                return False
        df_guide_current = df_guide_current[df_guide_current['Дата'].apply(check_date_match)]
    
    st.write(f"В таблице найдено **{len(df_guide_current)}** записей для гида {selected_guide} на {tab2_month_name} {tab2_year}.")
    
    if not df_guide_current.empty:
        for idx, row in df_guide_current.iterrows():
            col1, col2, col3, col4, col5, col6 = st.columns([2, 2, 1.5, 1.5, 1, 1])
            with col1: st.write(f"**{row['Дата']}**")
            with col2: st.write(f"{row['Время с']} - {row['Время до']}" if row['Доступен'] == 'да' else "Недоступен")
            with col6:
                if st.button("Удалить", key=f"del_{idx}"):
                    try:
                        spreadsheet = client.open_by_key(SHEET_ID)
                        ws_avail = spreadsheet.worksheet('Доступность')
                        cell = ws_avail.find(str(row['Дата']), in_column=2)
                        if cell:
                            ws_avail.delete_rows(cell.row)
                            st.success("Удалено!")
                            st.cache_data.clear()
                            st.rerun()
                    except Exception as e:
                        st.error(f"Ошибка: {e}")
    else:
        st.info(f"У этого гида пока нет записей на {tab2_month_name} {tab2_year}.")
        
    st.markdown("---")
    st.subheader(f"Добавить доступные дни на {tab2_month_name} {tab2_year}")
    
    max_days = calendar.monthrange(tab2_year, tab2_month)[1]
    
    if st.button("✨ Сделать доступным ВЕСЬ МЕСЯЦ"):
        try:
            spreadsheet = client.open_by_key(SHEET_ID)
            ws_avail = spreadsheet.worksheet('Доступность')
            all_records = ws_avail.get_all_values()
            rows_to_update = {row[1].strip(): i for i, row in enumerate(all_records[1:], start=2) if len(row) >= 2 and row[0].strip() == selected_guide}
            
            added = 0
            for day in range(1, max_days + 1):
                d = datetime.date(tab2_year, tab2_month, day)
                date_str = d.strftime('%d.%m.%Y')
                new_row = [selected_guide, date_str, "да", "00:00", "23:59"]
                if date_str in rows_to_update:
                    ws_avail.update(f'A{rows_to_update[date_str]}:E{rows_to_update[date_str]}', [new_row])
                else:
                    ws_avail.append_row(new_row)
                    added += 1
            st.success(f"Добавлено {added} дней (весь месяц).")
            st.cache_data.clear()
            st.rerun()
        except Exception as e:
            st.error(f"Ошибка: {e}")
    
    available_days = st.multiselect("Или выберите конкретные дни месяца:", options=list(range(1, max_days + 1)), default=[])
    is_all_day = st.checkbox("Доступен весь день (без ограничений по времени)", value=True)
    
    if not is_all_day:
        time_col1, time_col2 = st.columns(2)
        with time_col1: time_from = st.time_input("Время с:", value=datetime.time(11, 0), key="time_from")
        with time_col2: time_to = st.time_input("Время до:", value=datetime.time(18, 0), key="time_to")
    else:
        time_from = datetime.time(0, 0)
        time_to = datetime.time(23, 59)

    if st.button("💾 Сохранить выбранные дни", type="primary"):
        if not available_days:
            st.warning("Пожалуйста, выберите хотя бы один день.")
        else:
            try:
                spreadsheet = client.open_by_key(SHEET_ID)
                ws_avail = spreadsheet.worksheet('Доступность')
                all_records = ws_avail.get_all_values()
                rows_to_update = {row[1].strip(): i for i, row in enumerate(all_records[1:], start=2) if len(row) >= 2 and row[0].strip() == selected_guide}
                
                time_from_str = time_from.strftime('%H:%M')
                time_to_str = time_to.strftime('%H:%M')
                added_count, updated_count = 0, 0
                
                for day in available_days:
                    try: d = datetime.date(tab2_year, tab2_month, day)
                    except ValueError: continue
                    date_str = d.strftime('%d.%m.%Y')
                    new_row = [selected_guide, date_str, "да", time_from_str, time_to_str]
                    
                    if date_str in rows_to_update:
                        ws_avail.update(f'A{rows_to_update[date_str]}:E{rows_to_update[date_str]}', [new_row])
                        updated_count += 1
                    else:
                        ws_avail.append_row(new_row)
                        added_count += 1
                
                st.success(f"✅ Добавлено: {added_count}, Обновлено: {updated_count}.")
                st.cache_data.clear()
                st.rerun()
            except Exception as e:
                st.error(f"❌ Ошибка при сохранении: {e}")

# --- ВКЛАДКА 3: НАСТРОЙКИ ---
with tab3:
    st.header("⚙️ Настройки алгоритма")
    st.write("Здесь вы можете настроить приоритеты. Чем выше значение, тем важнее для алгоритма это правило.")
    
    with st.form("config_form"):
        st.subheader("Приоритеты (Бонусы)")
        new_presence = st.number_input("Провести КАЖДУЮ экскурсию хотя бы 1 раз:", value=current_config["presence_bonus"], step=100)
        new_quantity = st.number_input("Провести МАКСИМУМ экскурсий (набрать частоту):", value=current_config["quantity_bonus"], step=1)
        
        st.subheader("Штрафы (Чем больше, тем сильнее избегаем)")
        new_duplicate = st.number_input("Две одинаковые экскурсии у одного гида в один день:", value=current_config["duplicate_penalty"], step=50)
        new_spread = st.number_input("Слишком близкое проведение одной и той же экскурсии:", value=current_config["spread_penalty"], step=50)
        
        st.subheader("Временные рамки")
        new_rest = st.number_input("Перерыв между экскурсиями (в часах):", value=float(current_config["rest_time_hours"]), step=0.5)
        col_t1, col_t2, col_t3 = st.columns(3)
        with col_t1: new_start = st.number_input("Самый ранний старт (час):", value=current_config["start_time_hour"], min_value=0, max_value=23)
        with col_t2: new_end = st.number_input("Самый поздний старт (час):", value=current_config["end_time_hour"], min_value=0, max_value=23)
        with col_t3: new_step = st.number_input("Шаг подбора (мин):", value=current_config["time_step_minutes"], min_value=15, max_value=120, step=15)
            
        submit_config = st.form_submit_button("💾 Сохранить настройки")
        
        if submit_config:
            new_config_data = {
                "rest_time_hours": new_rest,
                "presence_bonus": new_presence,
                "quantity_bonus": new_quantity,
                "start_time_hour": new_start,
                "end_time_hour": new_end,
                "time_step_minutes": new_step,
                "duplicate_penalty": new_duplicate,
                "spread_penalty": new_spread
            }
            try:
                with open(CONFIG_FILE, "w") as f:
                    json.dump(new_config_data, f, indent=4)
                st.success("✅ Настройки сохранены!")
                current_config.update(new_config_data) 
            except Exception as e:
                st.error(f"Ошибка сохранения: {e}")

with st.sidebar:
    st.header("📊 Текущие данные")
    st.write("**Экскурсии:**")
    st.dataframe(df_tours[['Название экскурсии', 'Гиды']], hide_index=True)
