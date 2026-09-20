import random, sqlite3, os
from datetime import date, timedelta
import pandas as pd

random.seed(42)
TODAY = date(2026, 9, 8)
OUT = "/mnt/user-data/outputs/dental_dataset"
os.makedirs(OUT, exist_ok=True)

CHINESE_SUR = ["Tan","Lim","Lee","Ng","Ong","Wong","Goh","Chua","Chan","Koh","Teo","Ang","Yeo","Tay","Ho","Low","Toh","Sim","Chia","Neo"]
CHINESE_GIV = ["Wei Ming","Jia Hui","Kai Xin","Zhi Hao","Mei Ling","Jun Jie","Xin Yi","Wen Bin","Hui Min","Yong Sheng","Li Ting","Cheng Yu","Shu Fen","Jia Le","Bee Choo","Ah Seng","Poh Lin","Kok Wai","Siew Ling","Chee Keong"]
MALAY_SUR = ["bin Abdullah","bin Hassan","binte Rahman","binte Ismail","bin Osman","binte Yusof","bin Ibrahim","binte Salleh"]
MALAY_GIV = ["Nurul","Muhammad","Siti","Ahmad","Aisyah","Farhan","Nadia","Hafiz","Zulkifli","Syafiqah"]
INDIAN_SUR = ["Kumar","Rajan","Pillai","Naidu","Menon","Subramaniam","Devi","Sharma","Raj","Krishnan"]
INDIAN_GIV = ["Ravi","Priya","Suresh","Kavitha","Arun","Deepa","Vijay","Lakshmi","Anand","Meera"]
WESTERN_SUR = ["Smith","Anderson","Fernandez","Pereira","Clarke","Wright","De Souza","Martin"]
WESTERN_GIV = ["James","Sarah","Michael","Emma","David","Rachel","Daniel","Claire"]

def make_name():
    r = random.random()
    if r < 0.62:
        return f"{random.choice(CHINESE_GIV)} {random.choice(CHINESE_SUR)}"
    if r < 0.78:
        return f"{random.choice(MALAY_GIV)} {random.choice(MALAY_SUR)}"
    if r < 0.90:
        return f"{random.choice(INDIAN_GIV)} {random.choice(INDIAN_SUR)}"
    return f"{random.choice(WESTERN_GIV)} {random.choice(WESTERN_SUR)}"

def sg_phone():
    return f"+65{random.choice(['8','9'])}{random.randint(1000000,9999999)}"

DENTISTS = ["Dr Tan Wei Loong","Dr Priya Menon","Dr Sarah Lim","Dr Amir Rahman","Dr Jonathan Koh"]

RECALL_TYPES = [
    ("routine_hygiene", 6, 1),
    ("routine_hygiene", 12, 1),
    ("perio_maintenance", 3, 3),
    ("perio_maintenance", 4, 3),
    ("post_treatment_review", 3, 4),
    ("ortho_adjustment", 2, 3),
    ("implant_review", 6, 4),
    ("paediatric_checkup", 6, 2),
]

TREATMENTS = [
    ("scaling_polishing", "completed", 1),
    ("filling", "completed", 2),
    ("root_canal", "in_progress", 5),
    ("root_canal", "completed", 2),
    ("crown_fitting", "in_progress", 4),
    ("extraction", "completed", 2),
    ("wisdom_tooth_surgery", "completed", 3),
    ("implant_placement", "in_progress", 5),
    ("orthodontic_treatment", "in_progress", 3),
    ("periodontal_therapy", "in_progress", 4),
    ("denture_fitting", "in_progress", 3),
    ("whitening", "completed", 1),
]

N = 3200
patients, appointments, treatments, recalls, contacts = [], [], [], [], []
aid = tid = rid = cid = 1

for i in range(1, N + 1):
    pid = f"P{i:05d}"
    name = make_name()
    reg = TODAY - timedelta(days=random.randint(30, 3650))
    age = random.choices([random.randint(4,17), random.randint(18,44), random.randint(45,64), random.randint(65,88)], weights=[18,42,28,12])[0]
    dob = TODAY - timedelta(days=age*365 + random.randint(0,364))

    phone = sg_phone()
    email = f"{name.split()[0].lower()}{random.randint(1,999)}@{random.choice(['gmail.com','hotmail.com','yahoo.com.sg','outlook.com'])}"

    # --- injected mess ---
    if random.random() < 0.06: phone = ""                      # missing phone
    elif random.random() < 0.04: phone = phone.replace("+65","")  # inconsistent format
    elif random.random() < 0.02: phone = "91234567 / 98765432"   # two numbers in one field
    if random.random() < 0.18: email = ""                       # missing email
    if random.random() < 0.03: name = name.upper()              # inconsistent casing

    pref = random.choices(["whatsapp","sms","email","phone_call"], weights=[58,22,12,8])[0]
    wa_consent = random.random() > 0.09
    mk_consent = random.random() > 0.42
    if random.random() < 0.035:
        wa_consent = False; mk_consent = False; note = "Opted out of all contact"
    else:
        note = random.choice(["","","","","Prefers evening appointments","Anxious patient - allow extra time",
                              "Relocated overseas","Interpreter required (Mandarin)","Wheelchair access needed",
                              "Do not call during work hours","Contact via spouse"])

    status = "active"
    if random.random() < 0.04: status = "inactive"
    if random.random() < 0.008: status = "deceased"
    if note == "Relocated overseas": status = "overseas"

    patients.append(dict(patient_id=pid, full_name=name, date_of_birth=dob, phone=phone, email=email,
                         registration_date=reg, preferred_channel=pref,
                         whatsapp_consent=int(wa_consent), marketing_consent=int(mk_consent),
                         patient_status=status, notes=note))

    # --- appointment history ---
    n_appt = random.choices([0,1,2,3,4,5,6,8,10,14], weights=[3,10,14,16,15,12,10,10,6,4])[0]
    last_completed = None
    cursor = reg
    for _ in range(n_appt):
        cursor = cursor + timedelta(days=random.randint(60, 400))
        if cursor > TODAY: break
        st = random.choices(["completed","no_show","cancelled"], weights=[80,11,9])[0]
        appointments.append(dict(appointment_id=f"A{aid:06d}", patient_id=pid, appointment_date=cursor,
                                 appointment_type=random.choice(["checkup","hygiene","treatment","consultation","emergency","review"]),
                                 status=st, dentist=random.choice(DENTISTS),
                                 duration_min=random.choice([15,30,30,45,60,90])))
        aid += 1
        if st == "completed": last_completed = cursor

    # --- treatments ---
    for _ in range(random.choices([0,1,1,2,3],weights=[22,34,20,16,8])[0]):
        tname, tstat, urg = random.choice(TREATMENTS)
        start = last_completed or reg
        start = start - timedelta(days=random.randint(0, 500))
        if tstat == "in_progress" and random.random() < 0.28:
            tstat = "abandoned"
        treatments.append(dict(treatment_id=f"T{tid:06d}", patient_id=pid, treatment_type=tname,
                               start_date=start, treatment_status=tstat, clinical_urgency=urg,
                               dentist=random.choice(DENTISTS)))
        tid += 1

    # --- recall schedule ---
    rtype, interval, urg = random.choice(RECALL_TYPES)
    if age < 18: rtype, interval, urg = "paediatric_checkup", 6, 2
    # most active patients had a recent visit and are not yet due
    if last_completed and random.random() < 0.62:
        last_completed = TODAY - timedelta(days=random.randint(5, int(interval*30*0.9)))
    if last_completed:
        due = last_completed + timedelta(days=interval*30)
    else:
        due = reg + timedelta(days=interval*30)
    if random.random() < 0.05:
        last_completed = None   # record with no visit history
    rstat = "due" if due <= TODAY else "scheduled"
    if random.random() < 0.04: rstat = "booked"
    recalls.append(dict(recall_id=f"R{rid:06d}", patient_id=pid, recall_type=rtype,
                        interval_months=interval, last_visit_date=last_completed,
                        next_due_date=due, recall_status=rstat, base_urgency=urg))
    rid += 1

    # --- contact log ---
    for _ in range(random.choices([0,1,2,3,5],weights=[30,28,20,14,8])[0]):
        cdate = TODAY - timedelta(days=random.randint(1, 900))
        direction = random.choices(["outbound","inbound"], weights=[72,28])[0]
        if direction == "outbound":
            outcome = random.choices(["delivered","no_response","booked","failed","opted_out"], weights=[40,30,18,8,4])[0]
            msg = random.choice(["recall_reminder","appointment_reminder","treatment_followup","rescheduling"])
        else:
            outcome = random.choices(["reschedule_request","deferral","question","booked","complaint"], weights=[34,26,22,14,4])[0]
            msg = "patient_reply"
        contacts.append(dict(contact_id=f"C{cid:06d}", patient_id=pid, contact_date=cdate,
                             channel=random.choices(["whatsapp","sms","email","phone_call"],weights=[55,22,13,10])[0],
                             direction=direction, message_type=msg, outcome=outcome))
        cid += 1

# --- duplicate patients (same human, two records) ---
dupes = random.sample(patients, 55)
for d in dupes:
    N += 1
    nd = dict(d)
    nd["patient_id"] = f"P{N:05d}"
    nm = d["full_name"]
    nd["full_name"] = nm.replace(" ", "  ") if random.random() < 0.3 else nm.title()
    if random.random() < 0.5: nd["email"] = ""
    nd["registration_date"] = d["registration_date"] + timedelta(days=random.randint(200, 1500))
    nd["notes"] = "Possible duplicate record"
    patients.append(nd)
    r = dict(random.choice(recalls)); r["recall_id"] = f"R{rid:06d}"; r["patient_id"] = nd["patient_id"]
    recalls.append(r); rid += 1

dfs = {
    "patients": pd.DataFrame(patients),
    "appointments": pd.DataFrame(appointments),
    "treatments": pd.DataFrame(treatments),
    "recall_schedule": pd.DataFrame(recalls),
    "contact_log": pd.DataFrame(contacts),
}

for name, df in dfs.items():
    df.to_csv(f"{OUT}/{name}.csv", index=False)

conn = sqlite3.connect(f"{OUT}/clinic.db")
for name, df in dfs.items():
    df.to_sql(name, conn, if_exists="replace", index=False)
conn.executescript("""
CREATE INDEX idx_appt_patient ON appointments(patient_id);
CREATE INDEX idx_treat_patient ON treatments(patient_id);
CREATE INDEX idx_recall_due ON recall_schedule(next_due_date);
CREATE INDEX idx_contact_patient ON contact_log(patient_id);
""")
conn.commit()

overdue = dfs["recall_schedule"]
od = overdue[(pd.to_datetime(overdue.next_due_date) < pd.Timestamp(TODAY)) & (overdue.recall_status != "booked")]
print("patients:", len(dfs['patients']))
print("appointments:", len(dfs['appointments']))
print("treatments:", len(dfs['treatments']))
print("recalls:", len(dfs['recall_schedule']), "| overdue:", len(od))
print("contact_log:", len(dfs['contact_log']))
print("missing phone:", (dfs['patients'].phone == "").sum())
print("no whatsapp consent:", (dfs['patients'].whatsapp_consent == 0).sum())
print("abandoned treatments:", (dfs['treatments'].treatment_status == 'abandoned').sum())
conn.close()
