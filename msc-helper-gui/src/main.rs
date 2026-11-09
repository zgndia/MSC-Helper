//! MSC Helper – GUI rewritten in Rust (egui).
//! Talks to the existing Python backend over a tiny local HTTP control plane.
//! Overlay and all backend logic remain in Python.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use eframe::egui::{self, Color32, FontId, RichText, Sense, Stroke, Vec2};
use serde::{Deserialize, Serialize};
use std::time::{Duration, Instant};

const APP_NAME: &str = "MSC Helper";
const APP_VERSION: &str = "0.3.0";
const CONTROL_URL: &str = "http://127.0.0.1:17890";

// ── Theme (matches the original dark theme) ──
const BG: Color32 = Color32::from_rgb(0x17, 0x17, 0x17);
const PANEL: Color32 = Color32::from_rgb(0x24, 0x24, 0x24);
const PANEL_ALT: Color32 = Color32::from_rgb(0x2b, 0x2b, 0x2b);
const BORDER: Color32 = Color32::from_rgb(0x3d, 0x3d, 0x3d);
const TEXT: Color32 = Color32::from_rgb(0xee, 0xee, 0xee);
const MUTED: Color32 = Color32::from_rgb(0xa9, 0xa9, 0xa9);
const RED: Color32 = Color32::from_rgb(0xe8, 0x79, 0x79);
const GREEN: Color32 = Color32::from_rgb(0x7f, 0xc4, 0x9a);
const AMBER: Color32 = Color32::from_rgb(0xdf, 0xb9, 0x6e);
const MANA: Color32 = Color32::from_rgb(0x80, 0xb9, 0x9d);

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
#[serde(default)]
struct Telemetry {
    focused: bool,
    max_hp: i32,
    max_mp: i32,
    movement_detected: bool,
    ability_listening: bool,
    movement_keys: String,
    enabled_commands: Vec<String>,
    hp_ratio: Option<f32>,
    hp_current: Option<i32>,
    hp_max: Option<i32>,
    hp_exact: bool,
    hp_last_seen: Option<String>,
    hp_age: Option<f32>,
    hp_fresh: bool,
    mp_ratio: Option<f32>,
    mp_current: Option<i32>,
    mp_max: Option<i32>,
    mp_exact: bool,
    mp_last_seen: Option<String>,
    mp_age: Option<f32>,
    mp_fresh: bool,
    status_text: String,
    status_color: String,
    log_lines: Vec<String>,
    target_info: String,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize, PartialEq)]
#[serde(default)]
struct SettingsDto {
    hp_enabled: bool,
    mp_enabled: bool,
    hp_key: String,
    mp_key: String,
    max_hp: i32,
    max_mp: i32,
    hp_threshold: f64,
    mp_threshold: f64,
    hp_threshold_mode: String,
    mp_threshold_mode: String,
    hp_random_offset: i32,
    mp_random_offset: i32,
    ability_spam_enabled: bool,
    ability_keys: String,
    movement_keys: String,
    ability_interval_ms: i32,
    ability_set_delay_ms: i32,
    movement_hold_ms: i32,
    pause_spam_while_typing: bool,
    overlay_enabled: bool,
    window_title: String,
    potion_cooldown_ms: i32,
    hud_scan_interval_ms: i32,
    key_hold_ms: i32,
    auto_detect_max_stats: bool,
    hud_monitoring_enabled: bool,
    passive_hud_reading: bool,
    min_focus_hold_ms: i32,
    auto_update_enabled: bool,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
struct StatusResponse {
    ok: bool,
    telemetry: Telemetry,
    settings: SettingsDto,
}

#[derive(PartialEq, Eq)]
enum Tab {
    General,
    AutoPot,
    AutoCast,
    Overlay,
    Settings,
}

struct MscHelperApp {
    tab: Tab,
    telemetry: Telemetry,
    settings: SettingsDto,
    edit: SettingsDto,
    last_poll: Instant,
    poll_interval: Duration,
    connected: bool,
    status_message: String,
    log: Vec<String>,
    /// Settings changed locally; push after a short debounce.
    dirty_since: Option<Instant>,
    last_pushed: SettingsDto,
}

impl Default for MscHelperApp {
    fn default() -> Self {
        Self {
            tab: Tab::General,
            telemetry: Telemetry {
                status_text: "Waiting for backend…".into(),
                status_color: "amber".into(),
                target_info: "MSC Helper is searching for MapleStory.exe. Make sure MapleStory Classic is running.".into(),
                ..Default::default()
            },
            settings: SettingsDto::default(),
            edit: SettingsDto::default(),
            last_poll: Instant::now() - Duration::from_secs(10),
            poll_interval: Duration::from_millis(180),
            connected: false,
            status_message: String::new(),
            log: Vec::new(),
            dirty_since: None,
            last_pushed: SettingsDto::default(),
        }
    }
}

impl MscHelperApp {
    fn mark_dirty(&mut self) {
        self.dirty_since = Some(Instant::now());
    }

    fn poll_backend(&mut self) {
        if self.last_poll.elapsed() < self.poll_interval {
            return;
        }
        self.last_poll = Instant::now();

        match ureq::get(&format!("{CONTROL_URL}/status"))
            .timeout(Duration::from_millis(120))
            .call()
        {
            Ok(resp) => match resp.into_json::<StatusResponse>() {
                Ok(body) if body.ok => {
                    self.connected = true;
                    self.telemetry = body.telemetry;
                    self.settings = body.settings.clone();
                    // Don't overwrite local edits while dirty.
                    if self.dirty_since.is_none() {
                        self.edit = body.settings.clone();
                        self.last_pushed = body.settings;
                    }
                    // Server sends the full recent window each poll — replace, don't append.
                    self.log = self.telemetry.log_lines.clone();
                }
                _ => {
                    self.connected = false;
                    self.telemetry.status_text = "Backend returned bad data".into();
                    self.telemetry.status_color = "red".into();
                }
            },
            Err(_) => {
                self.connected = false;
                self.telemetry.status_text = "Waiting for Python backend…".into();
                self.telemetry.status_color = "amber".into();
            }
        }
    }

    /// Auto-save: push settings ~350 ms after the last edit.
    fn auto_save_tick(&mut self) {
        let Some(since) = self.dirty_since else {
            return;
        };
        if since.elapsed() < Duration::from_millis(350) {
            return;
        }
        if self.edit == self.last_pushed {
            self.dirty_since = None;
            return;
        }
        let payload = serde_json::json!({ "settings": self.edit });
        match ureq::post(&format!("{CONTROL_URL}/settings"))
            .timeout(Duration::from_millis(400))
            .send_json(payload)
        {
            Ok(_) => {
                self.last_pushed = self.edit.clone();
                self.settings = self.edit.clone();
                self.dirty_since = None;
                self.status_message.clear();
            }
            Err(e) => {
                self.status_message = format!("Auto-save failed: {e}");
                // Retry later
                self.dirty_since = Some(Instant::now());
            }
        }
    }

    fn status_color(&self) -> Color32 {
        match self.telemetry.status_color.as_str() {
            "green" => GREEN,
            "red" => RED,
            "muted" => MUTED,
            _ => AMBER,
        }
    }
}

impl eframe::App for MscHelperApp {
    fn update(&mut self, ctx: &egui::Context, _frame: &mut eframe::Frame) {
        ctx.request_repaint_after(Duration::from_millis(160));
        self.poll_backend();
        self.auto_save_tick();

        let mut style = (*ctx.style()).clone();
        style.visuals.dark_mode = true;
        style.visuals.panel_fill = BG;
        style.visuals.window_fill = BG;
        style.visuals.extreme_bg_color = PANEL;
        style.visuals.widgets.noninteractive.bg_fill = PANEL;
        style.visuals.widgets.inactive.bg_fill = PANEL_ALT;
        style.visuals.widgets.hovered.bg_fill = Color32::from_rgb(0x3a, 0x3a, 0x3a);
        style.visuals.widgets.active.bg_fill = Color32::from_rgb(0x45, 0x45, 0x45);
        style.visuals.selection.bg_fill = Color32::from_rgb(0x3a, 0x5a, 0x7a);
        style.visuals.override_text_color = Some(TEXT);
        style.spacing.item_spacing = Vec2::new(8.0, 6.0);
        ctx.set_style(style);

        egui::CentralPanel::default()
            .frame(egui::Frame::none().fill(BG).inner_margin(egui::Margin::symmetric(16.0, 12.0)))
            .show(ctx, |ui| {
                // Header
                ui.horizontal(|ui| {
                    ui.label(
                        RichText::new("MSC HELPER")
                            .font(FontId::proportional(22.0))
                            .strong()
                            .color(TEXT),
                    );
                    ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                        let badge = if self.connected {
                            format!("●  {}", self.telemetry.status_text.to_uppercase())
                        } else {
                            "●  WAITING".to_string()
                        };
                        ui.label(
                            RichText::new(badge)
                                .font(FontId::proportional(12.0))
                                .strong()
                                .color(self.status_color()),
                        );
                    });
                });
                ui.label(
                    RichText::new("MapleStory Classic Helper")
                        .font(FontId::proportional(12.0))
                        .color(MUTED),
                );
                ui.add_space(6.0);

                // Tabs
                ui.horizontal(|ui| {
                    tab_button(ui, &mut self.tab, Tab::General, "General");
                    tab_button(ui, &mut self.tab, Tab::AutoPot, "Auto-Pot");
                    tab_button(ui, &mut self.tab, Tab::AutoCast, "Auto-Cast");
                    tab_button(ui, &mut self.tab, Tab::Overlay, "Overlay");
                    tab_button(ui, &mut self.tab, Tab::Settings, "Settings");
                });
                ui.add_space(4.0);
                ui.separator();
                ui.add_space(6.0);

                // Tab body fills remaining height
                let body_h = ui.available_height();
                egui::ScrollArea::vertical()
                    .auto_shrink([false, false])
                    .max_height(body_h)
                    .show(ui, |ui| {
                        ui.set_min_height(body_h - 4.0);
                        match self.tab {
                            Tab::General => self.ui_general(ui),
                            Tab::AutoPot => self.ui_auto_pot(ui),
                            Tab::AutoCast => self.ui_auto_cast(ui),
                            Tab::Overlay => self.ui_overlay(ui),
                            Tab::Settings => self.ui_settings(ui),
                        }
                    });
            });
    }
}

fn tab_button(ui: &mut egui::Ui, current: &mut Tab, target: Tab, label: &str) {
    let selected = *current == target;
    let fill = if selected {
        PANEL
    } else {
        Color32::from_rgb(0x29, 0x29, 0x29)
    };
    let text_color = if selected { TEXT } else { MUTED };
    let response = ui.add(
        egui::Button::new(RichText::new(label).color(text_color).strong())
            .fill(fill)
            .stroke(Stroke::NONE)
            .min_size(Vec2::new(88.0, 26.0)),
    );
    if response.clicked() {
        *current = target;
    }
}

fn card_frame() -> egui::Frame {
    egui::Frame::none()
        .fill(PANEL)
        .stroke(Stroke::new(1.0_f32, BORDER))
        .inner_margin(egui::Margin::same(12.0))
        .rounding(4.0)
}

fn card_title(ui: &mut egui::Ui, title: &str) {
    ui.label(
        RichText::new(title.to_uppercase())
            .font(FontId::proportional(11.0))
            .color(MUTED),
    );
    ui.add_space(4.0);
}

fn meter_bar(ui: &mut egui::Ui, ratio: f32, color: Color32, height: f32) {
    let desired = Vec2::new(ui.available_width(), height);
    let (rect, _) = ui.allocate_exact_size(desired, Sense::hover());
    let painter = ui.painter();
    painter.rect_filled(rect, 3.0, Color32::from_rgb(0x19, 0x19, 0x19));
    let fill_w = (rect.width() * ratio.clamp(0.0, 1.0)).max(0.0);
    if fill_w > 0.5 {
        let mut fill_rect = rect;
        fill_rect.set_width(fill_w);
        painter.rect_filled(fill_rect, 3.0, color);
    }
    painter.rect_stroke(rect, 3.0, Stroke::new(1.0_f32, BORDER));
}

impl MscHelperApp {
    fn ui_general(&mut self, ui: &mut egui::Ui) {
        // Top row: HP | MP  (fixed-ish height)
        ui.columns(2, |cols| {
            card_frame().show(&mut cols[0], |ui| {
                card_title(ui, "Health");
                let current = self.telemetry.hp_current.unwrap_or(0);
                let max = self.telemetry.hp_max.unwrap_or(self.telemetry.max_hp).max(1);
                let ratio = self.telemetry.hp_ratio.unwrap_or(0.0);
                let exact = if self.telemetry.hp_exact { "" } else { " ~" };
                ui.label(
                    RichText::new(format!("HP  {current} / {max}{exact}"))
                        .font(FontId::proportional(15.0))
                        .strong()
                        .color(TEXT),
                );
                ui.add_space(4.0);
                meter_bar(ui, ratio, RED, 12.0);
                ui.add_space(2.0);
                let freshness = if self.telemetry.hp_fresh {
                    "LIVE".to_string()
                } else if let Some(seen) = &self.telemetry.hp_last_seen {
                    format!("Last seen {seen}")
                } else {
                    "WAITING FOR HUD".to_string()
                };
                ui.label(RichText::new(freshness).color(MUTED).small());
            });
            card_frame().show(&mut cols[1], |ui| {
                card_title(ui, "Mana");
                let current = self.telemetry.mp_current.unwrap_or(0);
                let max = self.telemetry.mp_max.unwrap_or(self.telemetry.max_mp).max(1);
                let ratio = self.telemetry.mp_ratio.unwrap_or(0.0);
                let exact = if self.telemetry.mp_exact { "" } else { " ~" };
                ui.label(
                    RichText::new(format!("MP  {current} / {max}{exact}"))
                        .font(FontId::proportional(15.0))
                        .strong()
                        .color(TEXT),
                );
                ui.add_space(4.0);
                meter_bar(ui, ratio, MANA, 12.0);
                ui.add_space(2.0);
                let freshness = if self.telemetry.mp_fresh {
                    "LIVE".to_string()
                } else if let Some(seen) = &self.telemetry.mp_last_seen {
                    format!("Last seen {seen}")
                } else {
                    "WAITING FOR HUD".to_string()
                };
                ui.label(RichText::new(freshness).color(MUTED).small());
            });
        });

        ui.add_space(6.0);

        // Middle row: Game window | Enabled commands (equal min height)
        let mid_h = 96.0_f32;
        ui.columns(2, |cols| {
            card_frame().show(&mut cols[0], |ui| {
                ui.set_min_height(mid_h);
                card_title(ui, "Game window");
                ui.label(
                    RichText::new(&self.telemetry.target_info)
                        .color(TEXT)
                        .font(FontId::proportional(12.0)),
                );
            });
            card_frame().show(&mut cols[1], |ui| {
                ui.set_min_height(mid_h);
                card_title(ui, "Enabled Command List");
                if self.telemetry.enabled_commands.is_empty() {
                    ui.label(RichText::new("None enabled").color(MUTED));
                } else {
                    for cmd in &self.telemetry.enabled_commands {
                        ui.label(RichText::new(format!("• {cmd}")).color(TEXT));
                    }
                }
            });
        });

        ui.add_space(6.0);

        // Activity log — expand to fill remaining vertical space
        let log_h = (ui.available_height() - 8.0).max(160.0);
        card_frame().show(ui, |ui| {
            card_title(ui, "Recent activity");
            egui::ScrollArea::vertical()
                .max_height(log_h)
                .auto_shrink([false, false])
                .stick_to_bottom(true)
                .show(ui, |ui| {
                    ui.set_min_width(ui.available_width());
                    if self.log.is_empty() {
                        ui.label(RichText::new("No activity yet.").color(MUTED));
                    } else {
                        for line in &self.log {
                            ui.label(
                                RichText::new(line)
                                    .font(FontId::monospace(11.0))
                                    .color(Color32::from_rgb(0xdd, 0xdd, 0xdd)),
                            );
                        }
                    }
                });
        });
    }

    fn ui_auto_pot(&mut self, ui: &mut egui::Ui) {
        ui.columns(2, |cols| {
            card_frame().show(&mut cols[0], |ui| {
                card_title(ui, "Health potion");
                if ui
                    .checkbox(&mut self.edit.hp_enabled, "Toggle auto HP potion")
                    .changed()
                {
                    self.mark_dirty();
                }
                ui.add_space(4.0);
                if field_text(ui, "Key", &mut self.edit.hp_key) {
                    self.mark_dirty();
                }
                if field_i32(ui, "Max HP", &mut self.edit.max_hp) {
                    self.mark_dirty();
                }
                if field_f64(ui, "Threshold", &mut self.edit.hp_threshold) {
                    self.mark_dirty();
                }
                if field_mode(ui, "Threshold type", &mut self.edit.hp_threshold_mode) {
                    self.mark_dirty();
                }
                if field_i32(ui, "Random offset", &mut self.edit.hp_random_offset) {
                    self.mark_dirty();
                }
            });
            card_frame().show(&mut cols[1], |ui| {
                card_title(ui, "Mana potion");
                if ui
                    .checkbox(&mut self.edit.mp_enabled, "Toggle auto MP potion")
                    .changed()
                {
                    self.mark_dirty();
                }
                ui.add_space(4.0);
                if field_text(ui, "Key", &mut self.edit.mp_key) {
                    self.mark_dirty();
                }
                if field_i32(ui, "Max MP", &mut self.edit.max_mp) {
                    self.mark_dirty();
                }
                if field_f64(ui, "Threshold", &mut self.edit.mp_threshold) {
                    self.mark_dirty();
                }
                if field_mode(ui, "Threshold type", &mut self.edit.mp_threshold_mode) {
                    self.mark_dirty();
                }
                if field_i32(ui, "Random offset", &mut self.edit.mp_random_offset) {
                    self.mark_dirty();
                }
            });
        });
        ui.add_space(6.0);
        ui.label(
            RichText::new("Changes are saved automatically.")
                .color(MUTED)
                .small(),
        );
    }

    fn ui_auto_cast(&mut self, ui: &mut egui::Ui) {
        card_frame().show(ui, |ui| {
            card_title(ui, "Auto-cast / ability spam");
            if ui
                .checkbox(
                    &mut self.edit.ability_spam_enabled,
                    "Enable auto-cast while moving",
                )
                .changed()
            {
                self.mark_dirty();
            }
            ui.add_space(6.0);
            if field_text(ui, "Movement keys (comma-separated)", &mut self.edit.movement_keys)
            {
                self.mark_dirty();
            }
            if field_text(ui, "Ability keys (comma-separated)", &mut self.edit.ability_keys) {
                self.mark_dirty();
            }
            if field_i32(ui, "Delay between each cast (ms)", &mut self.edit.ability_interval_ms)
            {
                self.mark_dirty();
            }
            if field_i32(
                ui,
                "Delay after ability set (ms)",
                &mut self.edit.ability_set_delay_ms,
            ) {
                self.mark_dirty();
            }
            if field_i32(ui, "Continuous key-hold gate (ms)", &mut self.edit.movement_hold_ms)
            {
                self.mark_dirty();
            }
            if ui
                .checkbox(
                    &mut self.edit.pause_spam_while_typing,
                    "Pause while typing in chat",
                )
                .changed()
            {
                self.mark_dirty();
            }
        });
        ui.add_space(6.0);
        ui.label(
            RichText::new("Changes are saved automatically.")
                .color(MUTED)
                .small(),
        );
    }

    fn ui_overlay(&mut self, ui: &mut egui::Ui) {
        card_frame().show(ui, |ui| {
            card_title(ui, "Live game overlay");
            if ui
                .checkbox(&mut self.edit.overlay_enabled, "Show live status overlay")
                .changed()
            {
                self.mark_dirty();
            }
            ui.add_space(4.0);
            ui.label(
                RichText::new(
                    "The overlay is drawn by the Python process. Toggling this only tells the backend to show or hide it.",
                )
                .color(MUTED)
                .small(),
            );
        });
        ui.add_space(6.0);
        ui.label(
            RichText::new("Changes are saved automatically.")
                .color(MUTED)
                .small(),
        );
    }

    fn ui_settings(&mut self, ui: &mut egui::Ui) {
        card_frame().show(ui, |ui| {
            card_title(ui, "General options");
            if field_text(ui, "Window title filter", &mut self.edit.window_title) {
                self.mark_dirty();
            }
            if field_i32(ui, "Potion cooldown (ms)", &mut self.edit.potion_cooldown_ms) {
                self.mark_dirty();
            }
            if field_i32(ui, "HUD scan interval (ms)", &mut self.edit.hud_scan_interval_ms)
            {
                self.mark_dirty();
            }
            if field_i32(ui, "Key hold duration (ms)", &mut self.edit.key_hold_ms) {
                self.mark_dirty();
            }
            if field_i32(ui, "Min focus hold (ms)", &mut self.edit.min_focus_hold_ms) {
                self.mark_dirty();
            }
            ui.add_space(4.0);
            if ui
                .checkbox(
                    &mut self.edit.hud_monitoring_enabled,
                    "HUD monitoring enabled",
                )
                .changed()
            {
                self.mark_dirty();
            }
            if ui
                .checkbox(
                    &mut self.edit.auto_detect_max_stats,
                    "Auto-detect max HP/MP (OCR)",
                )
                .changed()
            {
                self.mark_dirty();
            }
            if ui
                .checkbox(
                    &mut self.edit.passive_hud_reading,
                    "Passive HUD reading (background)",
                )
                .changed()
            {
                self.mark_dirty();
            }
            if ui
                .checkbox(
                    &mut self.edit.auto_update_enabled,
                    "Automatic update checks",
                )
                .changed()
            {
                self.mark_dirty();
            }
        });

        ui.add_space(8.0);
        ui.horizontal(|ui| {
            if ui
                .add(
                    egui::Button::new("Reset to defaults")
                        .fill(Color32::from_rgb(0x3b, 0x3b, 0x3b))
                        .min_size(Vec2::new(140.0, 28.0)),
                )
                .clicked()
            {
                let _ = ureq::post(&format!("{CONTROL_URL}/reset"))
                    .timeout(Duration::from_millis(300))
                    .call();
                self.dirty_since = None;
                self.status_message = "Reset requested".into();
            }
            if ui
                .add(
                    egui::Button::new("Go to AppData folder")
                        .fill(Color32::from_rgb(0x3b, 0x3b, 0x3b))
                        .min_size(Vec2::new(160.0, 28.0)),
                )
                .clicked()
            {
                match open_appdata_folder() {
                    Ok(path) => {
                        self.status_message = format!("Opened {}", path.display());
                    }
                    Err(e) => {
                        self.status_message = format!("Could not open AppData folder: {e}");
                    }
                }
            }
        });
        ui.add_space(4.0);
        ui.label(
            RichText::new("Changes are saved automatically. Settings live under %APPDATA%\\MSCHelper.")
                .color(MUTED)
                .small(),
        );
        if !self.status_message.is_empty() {
            ui.label(RichText::new(&self.status_message).color(AMBER).small());
        }
    }
}


fn open_appdata_folder() -> Result<std::path::PathBuf, String> {
    let appdata = std::env::var("APPDATA").map_err(|_| "APPDATA is not set".to_string())?;
    let path = std::path::PathBuf::from(appdata).join("MSCHelper");
    std::fs::create_dir_all(&path).map_err(|e| e.to_string())?;
    // explorer.exe opens the folder in File Explorer on Windows.
    std::process::Command::new("explorer")
        .arg(&path)
        .spawn()
        .map_err(|e| e.to_string())?;
    Ok(path)
}

fn field_text(ui: &mut egui::Ui, label: &str, value: &mut String) -> bool {
    ui.label(RichText::new(label).color(MUTED).small());
    let resp = ui.add(egui::TextEdit::singleline(value).desired_width(f32::INFINITY));
    ui.add_space(2.0);
    resp.changed()
}

fn field_i32(ui: &mut egui::Ui, label: &str, value: &mut i32) -> bool {
    ui.label(RichText::new(label).color(MUTED).small());
    let resp = ui.add(egui::DragValue::new(value).speed(1.0));
    ui.add_space(2.0);
    resp.changed()
}

fn field_f64(ui: &mut egui::Ui, label: &str, value: &mut f64) -> bool {
    ui.label(RichText::new(label).color(MUTED).small());
    let resp = ui.add(egui::DragValue::new(value).speed(1.0));
    ui.add_space(2.0);
    resp.changed()
}

fn field_mode(ui: &mut egui::Ui, label: &str, value: &mut String) -> bool {
    ui.label(RichText::new(label).color(MUTED).small());
    let mut changed = false;
    egui::ComboBox::from_id_source(label)
        .selected_text(match value.as_str() {
            "percent" => "Remaining percentage",
            _ => "Missing points",
        })
        .show_ui(ui, |ui| {
            if ui
                .selectable_value(value, "points".to_string(), "Missing points")
                .changed()
            {
                changed = true;
            }
            if ui
                .selectable_value(value, "percent".to_string(), "Remaining percentage")
                .changed()
            {
                changed = true;
            }
        });
    ui.add_space(2.0);
    changed
}

fn main() -> eframe::Result<()> {
    let options = eframe::NativeOptions {
        viewport: egui::ViewportBuilder::default()
            .with_inner_size([1020.0, 720.0])
            .with_min_inner_size([900.0, 640.0])
            .with_title(format!("{APP_NAME}  v{APP_VERSION}")),
        ..Default::default()
    };

    eframe::run_native(
        APP_NAME,
        options,
        Box::new(|_cc| Box::new(MscHelperApp::default())),
    )
}
