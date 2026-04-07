-- 1. Profiles table (extends auth.users)
create table if not exists public.profiles (
  id uuid references auth.users on delete cascade primary key,
  role text default 'user',  -- 'user' | 'viber' | 'admin'
  display_name text,
  handle text unique,
  bio text,
  gopay_number text,
  bounties_earned numeric default 0,
  checks_completed integer default 0,
  created_at timestamptz default now(),
  updated_at timestamptz default now()
);

-- Enable RLS on profiles
alter table public.profiles enable row level security;

-- Profiles Policies
create policy "Public profiles are viewable by everyone." on public.profiles
  for select using (true);

create policy "Users can insert their own profile." on public.profiles
  for insert with check (auth.uid() = id);

create policy "Users can update own profile." on public.profiles
  for update using (auth.uid() = id);

-- 2. Vibe Checks table
create table if not exists public.vibe_checks (
  id uuid primary key default gen_random_uuid(),
  tour_id text not null,
  viber_id uuid references public.profiles(id),
  status text default 'claimed',  -- 'claimed' | 'submitted' | 'approved' | 'rejected'
  bounty_amount numeric default 100000, -- Default IDR 100k
  claim_expires_at timestamptz,
  overall_rating integer check (overall_rating >= 1 and overall_rating <= 5),
  overall_notes text,
  submitted_at timestamptz,
  approved_at timestamptz,
  created_at timestamptz default now(),
  updated_at timestamptz default now()
);

-- Enable RLS on vibe_checks
alter table public.vibe_checks enable row level security;

-- Vibe Checks Policies
create policy "Vibe checks are viewable by everyone." on public.vibe_checks
  for select using (true);

create policy "Vibers can claim and update their own checks." on public.vibe_checks
  for all using (auth.uid() = viber_id);

-- 3. Vibe Check Stops table
create table if not exists public.vibe_check_stops (
  id uuid primary key default gen_random_uuid(),
  vibe_check_id uuid references public.vibe_checks(id) on delete cascade,
  stop_index integer not null,
  stop_name text,
  photo_paths text[],     -- Array of 3 paths
  video_path text,
  gps_lat numeric,
  gps_lng numeric,
  rating integer check (rating >= 1 and rating <= 5),
  is_accurate boolean default true,
  notes text,
  created_at timestamptz default now()
);

-- Enable RLS on vibe_check_stops
alter table public.vibe_check_stops enable row level security;

-- Vibe Check Stops Policies
create policy "Stops are viewable by everyone." on public.vibe_check_stops
  for select using (true);

create policy "Vibers can manage their own stops." on public.vibe_check_stops
  for all using (
    exists (
      select 1 from public.vibe_checks
      where id = vibe_check_stops.vibe_check_id
      and viber_id = auth.uid()
    )
  );

-- Function to handle user creation and profile setup
create or replace function public.handle_new_user()
returns trigger as $$
begin
  insert into public.profiles (id, display_name, handle)
  values (new.id, new.raw_user_meta_data->>'full_name', lower(replace(new.raw_user_meta_data->>'full_name', ' ', '_')));
  return new;
end;
$$ language plpgsql security definer;

-- Trigger to call handle_new_user on signup
-- Note: Already handled in app.py logic, but good as a backup/clean way
-- create trigger on_auth_user_created
--   after insert on auth.users
--   for each row execute procedure public.handle_new_user();

-- 4. Submitted Tours table (user-generated tour ideas awaiting admin review)
create table if not exists public.submitted_tours (
  id uuid primary key default gen_random_uuid(),
  temp_tour_id text not null,           -- matches the /static/generated/{id}.json filename
  title text not null,
  description text,
  location text,
  tags text[],
  stops jsonb,                          -- full stop array from Gemini output
  narrative text,
  system_prompt text,
  submitter_id uuid references public.profiles(id) on delete set null,
  submitter_name text,
  submitter_email text,
  status text default 'pending',        -- 'pending' | 'approved' | 'rejected'
  admin_notes text,
  submitted_at timestamptz default now(),
  reviewed_at timestamptz
);

-- Enable RLS
alter table public.submitted_tours enable row level security;

-- Anyone can submit (insert); only admins can read/update via service role key
create policy "Anyone can submit a tour." on public.submitted_tours
  for insert with check (true);

create policy "Submissions are publicly readable." on public.submitted_tours
  for select using (true);
