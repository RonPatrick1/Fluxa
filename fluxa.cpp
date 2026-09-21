#include <httplib.h>
#include <nlohmann/json.hpp>
#include <openssl/crypto.h>
#include <openssl/evp.h>
#include <openssl/hmac.h>
#include <openssl/rand.h>
#include <sqlite3.h>

#include <arpa/inet.h>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <termios.h>
#include <unistd.h>

#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <cmath>
#include <csignal>
#include <cstdint>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <functional>
#include <iomanip>
#include <iostream>
#include <limits>
#include <map>
#include <memory>
#include <mutex>
#include <optional>
#include <regex>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <thread>
#include <tuple>
#include <unordered_map>
#include <unordered_set>
#include <utility>
#include <variant>
#include <vector>

namespace fs = std::filesystem;
using json = nlohmann::json;
using namespace std::chrono_literals;

namespace {

constexpr std::string_view kVersion = "0.2.0-cpp";
constexpr std::string_view kCookieName = "__Secure-FluxaSession";
constexpr std::string_view kFredPlayerCookieName = "FluxaFredPlayerAccess";
constexpr std::int64_t kSessionSeconds = 30LL * 24 * 60 * 60;
constexpr std::int64_t kRememberedFredPlayerSeconds = 100LL * 365 * 24 * 60 * 60;

struct ApiError : std::runtime_error {
    int status;
    ApiError(int status_, const std::string& message) : std::runtime_error(message), status(status_) {}
};

struct LibraryConfig {
    std::string name;
    std::string kind;
    fs::path path;
};

struct FredPlayerArtworkConfig {
    bool enabled{};
    std::string host{"127.0.0.1"};
    int port{8790};
    fs::path music_dir;
    fs::path env_file;
    std::string auth_token;
};

struct Config {
    fs::path config_path;
    std::string host;
    int port{};
    fs::path data_dir;
    fs::path database_path;
    bool scan_on_start{};
    bool probe_on_start{true};
    std::vector<LibraryConfig> libraries;
    FredPlayerArtworkConfig fredplayer_artwork;
};

struct ArtworkResponse {
    std::string bytes;
    std::string content_type;
};

std::string read_file(const fs::path& path) {
    std::ifstream input(path, std::ios::binary);
    if (!input) throw std::runtime_error("Could not read " + path.string());
    return {std::istreambuf_iterator<char>(input), std::istreambuf_iterator<char>()};
}

void write_file(const fs::path& path, const std::string& value, mode_t mode = 0600) {
    fs::create_directories(path.parent_path());
    auto temporary = path;
    temporary += ".tmp-" + std::to_string(::getpid());
    {
        std::ofstream output(temporary, std::ios::binary | std::ios::trunc);
        if (!output) throw std::runtime_error("Could not write " + temporary.string());
        output.write(value.data(), static_cast<std::streamsize>(value.size()));
        output.flush();
        if (!output) throw std::runtime_error("Could not finish writing " + temporary.string());
    }
    ::chmod(temporary.c_str(), mode);
    fs::rename(temporary, path);
    ::chmod(path.c_str(), mode);
}

std::string dotenv_value(const fs::path& path, std::string_view wanted_key) {
    std::ifstream input(path);
    if (!input) return {};
    auto strip = [](std::string value) {
        auto first = value.find_first_not_of(" \t\r\n");
        if (first == std::string::npos) return std::string{};
        auto last = value.find_last_not_of(" \t\r\n");
        return value.substr(first, last - first + 1);
    };
    std::string line;
    while (std::getline(input, line)) {
        line = strip(std::move(line));
        if (line.empty() || line.front() == '#') continue;
        if (line.starts_with("export ")) line.erase(0, 7);
        auto equals = line.find('=');
        if (equals == std::string::npos || strip(line.substr(0, equals)) != wanted_key) continue;
        auto value = strip(line.substr(equals + 1));
        if (value.size() >= 2 && ((value.front() == '"' && value.back() == '"') ||
                                  (value.front() == '\'' && value.back() == '\''))) {
            value = value.substr(1, value.size() - 2);
        }
        return value;
    }
    return {};
}

Config load_config(const fs::path& requested) {
    Config config;
    config.config_path = fs::weakly_canonical(fs::absolute(requested));
    auto raw = json::parse(read_file(config.config_path));
    config.host = raw.value("host", "127.0.0.1");
    config.port = raw.value("port", 8097);
    if (config.host.empty() || config.port < 1 || config.port > 65535) {
        throw std::runtime_error("Invalid Fluxa host or port");
    }
    fs::path data = raw.value("data_dir", ".fluxa");
    config.data_dir = fs::weakly_canonical(data.is_absolute() ? data : config.config_path.parent_path() / data);
    config.database_path = config.data_dir / "fluxa.db";
    config.scan_on_start = raw.value("scan_on_start", false);
    config.probe_on_start = raw.value("probe_on_start", true);
    if (raw.contains("fredplayer_artwork") && raw["fredplayer_artwork"].is_object()) {
        const auto& artwork = raw["fredplayer_artwork"];
        config.fredplayer_artwork.enabled = artwork.value("enabled", false);
        config.fredplayer_artwork.host = artwork.value("host", "127.0.0.1");
        config.fredplayer_artwork.port = artwork.value("port", 8790);
        fs::path music_dir = artwork.value("music_dir", "");
        fs::path env_file = artwork.value("env_file", "");
        if (!music_dir.empty()) config.fredplayer_artwork.music_dir = fs::weakly_canonical(music_dir);
        if (!env_file.empty()) config.fredplayer_artwork.env_file = fs::weakly_canonical(env_file);
        if (config.fredplayer_artwork.enabled) {
            if (config.fredplayer_artwork.host.empty() || config.fredplayer_artwork.port < 1 ||
                config.fredplayer_artwork.port > 65535 || config.fredplayer_artwork.music_dir.empty() ||
                config.fredplayer_artwork.env_file.empty()) {
                throw std::runtime_error("FredPlayer artwork configuration is incomplete");
            }
            config.fredplayer_artwork.auth_token = dotenv_value(config.fredplayer_artwork.env_file, "AUTH_TOKEN");
            if (config.fredplayer_artwork.auth_token.empty()) {
                throw std::runtime_error("Could not read FredPlayer AUTH_TOKEN from " + config.fredplayer_artwork.env_file.string());
            }
        }
    }
    for (const auto& item : raw.at("libraries")) {
        LibraryConfig library{
            item.at("name").get<std::string>(),
            item.at("kind").get<std::string>(),
            fs::weakly_canonical(fs::path(item.at("path").get<std::string>()))
        };
        if (library.kind != "video" && library.kind != "music") {
            throw std::runtime_error("Library kind must be video or music");
        }
        config.libraries.push_back(std::move(library));
    }
    if (config.libraries.empty()) throw std::runtime_error("At least one library is required");
    return config;
}

std::string lower(std::string value) {
    std::transform(value.begin(), value.end(), value.begin(), [](unsigned char c) {
        return static_cast<char>(std::tolower(c));
    });
    return value;
}

std::string trim(std::string value) {
    auto first = value.find_first_not_of(" \t\r\n");
    if (first == std::string::npos) return {};
    auto last = value.find_last_not_of(" \t\r\n");
    return value.substr(first, last - first + 1);
}

std::string replace_all(std::string value, std::string_view from, std::string_view to) {
    std::size_t offset = 0;
    while ((offset = value.find(from, offset)) != std::string::npos) {
        value.replace(offset, from.size(), to);
        offset += to.size();
    }
    return value;
}

std::string html_escape(std::string value) {
    value = replace_all(std::move(value), "&", "&amp;");
    value = replace_all(std::move(value), "<", "&lt;");
    value = replace_all(std::move(value), ">", "&gt;");
    value = replace_all(std::move(value), "\"", "&quot;");
    value = replace_all(std::move(value), "'", "&#x27;");
    return value;
}

int hex_digit(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

std::string url_decode(std::string_view input, bool plus_space = true) {
    std::string output;
    output.reserve(input.size());
    for (std::size_t i = 0; i < input.size(); ++i) {
        if (input[i] == '%' && i + 2 < input.size()) {
            int high = hex_digit(input[i + 1]);
            int low = hex_digit(input[i + 2]);
            if (high >= 0 && low >= 0) {
                output.push_back(static_cast<char>((high << 4) | low));
                i += 2;
                continue;
            }
        }
        output.push_back(input[i] == '+' && plus_space ? ' ' : input[i]);
    }
    return output;
}

std::string url_encode(std::string_view input, bool keep_slash = false) {
    std::ostringstream output;
    output << std::uppercase << std::hex;
    for (unsigned char c : input) {
        if (std::isalnum(c) || c == '-' || c == '_' || c == '.' || c == '~' || (keep_slash && c == '/')) {
            output << c;
        } else {
            output << '%' << std::setw(2) << std::setfill('0') << static_cast<int>(c);
        }
    }
    return output.str();
}

std::unordered_map<std::string, std::string> parse_form(std::string_view body) {
    std::unordered_map<std::string, std::string> fields;
    std::size_t start = 0;
    while (start <= body.size()) {
        auto end = body.find('&', start);
        if (end == std::string_view::npos) end = body.size();
        auto pair = body.substr(start, end - start);
        auto equals = pair.find('=');
        auto key = url_decode(pair.substr(0, equals));
        auto value = equals == std::string_view::npos ? std::string{} : url_decode(pair.substr(equals + 1));
        fields[key] = value;
        if (end == body.size()) break;
        start = end + 1;
    }
    return fields;
}

std::string base64url(const unsigned char* data, std::size_t length) {
    std::string encoded(4 * ((length + 2) / 3), '\0');
    int written = EVP_EncodeBlock(reinterpret_cast<unsigned char*>(encoded.data()), data, static_cast<int>(length));
    encoded.resize(static_cast<std::size_t>(written));
    while (!encoded.empty() && encoded.back() == '=') encoded.pop_back();
    std::replace(encoded.begin(), encoded.end(), '+', '-');
    std::replace(encoded.begin(), encoded.end(), '/', '_');
    return encoded;
}

std::vector<unsigned char> base64url_decode(std::string value) {
    std::replace(value.begin(), value.end(), '-', '+');
    std::replace(value.begin(), value.end(), '_', '/');
    while (value.size() % 4) value.push_back('=');
    std::vector<unsigned char> decoded(value.size() / 4 * 3 + 3);
    int written = EVP_DecodeBlock(decoded.data(), reinterpret_cast<const unsigned char*>(value.data()), static_cast<int>(value.size()));
    if (written < 0) throw std::runtime_error("Invalid base64 data");
    std::size_t padding = 0;
    if (!value.empty() && value.back() == '=') ++padding;
    if (value.size() > 1 && value[value.size() - 2] == '=') ++padding;
    decoded.resize(static_cast<std::size_t>(written) - padding);
    return decoded;
}

std::string random_token(std::size_t bytes = 18) {
    std::vector<unsigned char> data(bytes);
    if (RAND_bytes(data.data(), static_cast<int>(data.size())) != 1) {
        throw std::runtime_error("Could not create secure random token");
    }
    return base64url(data.data(), data.size());
}

std::string iso_timestamp() {
    auto now = std::chrono::system_clock::now();
    std::time_t value = std::chrono::system_clock::to_time_t(now);
    std::tm utc{};
    gmtime_r(&value, &utc);
    std::ostringstream output;
    output << std::put_time(&utc, "%Y-%m-%dT%H:%M:%SZ");
    return output.str();
}

std::string shell_display(const std::vector<std::string>& command) {
    std::ostringstream output;
    for (std::size_t i = 0; i < command.size(); ++i) {
        if (i) output << ' ';
        output << '\'' << replace_all(command[i], "'", "'\\''") << '\'';
    }
    return output.str();
}

struct ProcessResult {
    int status{};
    std::string output;
};

ProcessResult run_capture(const std::vector<std::string>& command) {
    int pipefd[2];
    if (::pipe(pipefd) != 0) throw std::runtime_error("Could not create process pipe");
    pid_t pid = ::fork();
    if (pid < 0) {
        ::close(pipefd[0]); ::close(pipefd[1]);
        throw std::runtime_error("Could not fork process");
    }
    if (pid == 0) {
        ::dup2(pipefd[1], STDOUT_FILENO);
        ::dup2(pipefd[1], STDERR_FILENO);
        int nullfd = ::open("/dev/null", O_RDONLY);
        if (nullfd >= 0) ::dup2(nullfd, STDIN_FILENO);
        ::close(pipefd[0]); ::close(pipefd[1]);
        std::vector<char*> argv;
        argv.reserve(command.size() + 1);
        for (const auto& part : command) argv.push_back(const_cast<char*>(part.c_str()));
        argv.push_back(nullptr);
        ::execvp(argv[0], argv.data());
        _exit(127);
    }
    ::close(pipefd[1]);
    std::string output;
    std::array<char, 65536> buffer{};
    while (true) {
        ssize_t count = ::read(pipefd[0], buffer.data(), buffer.size());
        if (count > 0) output.append(buffer.data(), static_cast<std::size_t>(count));
        else if (count == 0) break;
        else if (errno != EINTR) break;
    }
    ::close(pipefd[0]);
    int status = 0;
    while (::waitpid(pid, &status, 0) < 0 && errno == EINTR) {}
    return {WIFEXITED(status) ? WEXITSTATUS(status) : 128, std::move(output)};
}

class Database {
public:
    explicit Database(const fs::path& path) : path_(path) {
        fs::create_directories(path.parent_path());
        if (sqlite3_open_v2(path.c_str(), &db_, SQLITE_OPEN_READWRITE | SQLITE_OPEN_CREATE | SQLITE_OPEN_FULLMUTEX, nullptr) != SQLITE_OK) {
            throw std::runtime_error("Could not open Fluxa database: " + std::string(sqlite3_errmsg(db_)));
        }
        sqlite3_busy_timeout(db_, 30000);
        execute_script(R"SQL(
            PRAGMA foreign_keys = ON;
            PRAGMA journal_mode = WAL;
            CREATE TABLE IF NOT EXISTS libraries (
                id INTEGER PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('video','music')),
                root_path TEXT NOT NULL UNIQUE, enabled INTEGER NOT NULL DEFAULT 1, last_scan_at TEXT, last_scan_error TEXT);
            CREATE TABLE IF NOT EXISTS media (
                id INTEGER PRIMARY KEY, library_id INTEGER NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
                path TEXT NOT NULL UNIQUE, relative_path TEXT NOT NULL, file_name TEXT NOT NULL, title TEXT NOT NULL,
                sort_title TEXT NOT NULL, media_type TEXT NOT NULL CHECK(media_type IN ('video','audio')),
                extension TEXT NOT NULL, size_bytes INTEGER NOT NULL, modified_ns INTEGER NOT NULL,
                file_device INTEGER, file_inode INTEGER,
                available INTEGER NOT NULL DEFAULT 1, last_seen_scan TEXT NOT NULL, probe_status TEXT NOT NULL DEFAULT 'pending',
                probe_error TEXT, duration_ms INTEGER, container TEXT, video_codec TEXT, width INTEGER, height INTEGER,
                audio_codec TEXT, audio_channels INTEGER, audio_tracks INTEGER, subtitle_tracks INTEGER,
                chapters_json TEXT, subtitle_streams_json TEXT, show_title TEXT, season_number INTEGER, episode_number INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
            CREATE INDEX IF NOT EXISTS media_library_idx ON media(library_id,available);
            CREATE INDEX IF NOT EXISTS media_sort_idx ON media(sort_title);
            CREATE INDEX IF NOT EXISTS media_show_idx ON media(show_title,season_number,episode_number);
            CREATE TABLE IF NOT EXISTS playback_progress (
                media_id INTEGER PRIMARY KEY REFERENCES media(id) ON DELETE CASCADE, position_ms INTEGER NOT NULL DEFAULT 0,
                duration_ms INTEGER NOT NULL DEFAULT 0, completed INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS loudness_analyses (
                media_id INTEGER PRIMARY KEY REFERENCES media(id) ON DELETE CASCADE, analyzer_version INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'pending', integrated_lufs REAL, true_peak_db REAL, sample_interval_ms INTEGER,
                envelope_json TEXT, spike_segments_json TEXT, error TEXT, analyzed_at TEXT,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS global_compressor_settings (
                id INTEGER PRIMARY KEY CHECK (id = 1), enabled INTEGER NOT NULL DEFAULT 1,
                threshold_db REAL NOT NULL DEFAULT -24, ratio REAL NOT NULL DEFAULT 8,
                output_gain_db REAL NOT NULL DEFAULT 9,
                ceiling_db REAL NOT NULL DEFAULT -3, attack_ms REAL NOT NULL DEFAULT 15,
                release_ms REAL NOT NULL DEFAULT 750, knee REAL NOT NULL DEFAULT 4,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
            INSERT OR IGNORE INTO global_compressor_settings(id) VALUES (1);
            CREATE TABLE IF NOT EXISTS media_compressor_settings (
                media_id INTEGER PRIMARY KEY REFERENCES media(id) ON DELETE CASCADE,
                enabled INTEGER NOT NULL, threshold_db REAL NOT NULL, ratio REAL NOT NULL,
                output_gain_db REAL NOT NULL DEFAULT 9,
                ceiling_db REAL NOT NULL, attack_ms REAL NOT NULL, release_ms REAL NOT NULL,
                knee REAL NOT NULL, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS playlists (
                id INTEGER PRIMARY KEY, source TEXT NOT NULL, source_id TEXT NOT NULL, title TEXT NOT NULL,
                sort_title TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'mixed' CHECK(kind IN ('video','audio','mixed')),
                source_item_count INTEGER NOT NULL DEFAULT 0, matched_item_count INTEGER NOT NULL DEFAULT 0,
                source_updated_at INTEGER, imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(source,source_id));
            CREATE INDEX IF NOT EXISTS playlists_sort_idx ON playlists(sort_title,id);
            CREATE TABLE IF NOT EXISTS playlist_items (
                id INTEGER PRIMARY KEY, playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
                position INTEGER NOT NULL, media_id INTEGER REFERENCES media(id) ON DELETE SET NULL,
                source_item_id TEXT, source_order REAL, source_path TEXT, source_title TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(playlist_id,position));
            CREATE INDEX IF NOT EXISTS playlist_items_playlist_idx ON playlist_items(playlist_id,position);
            CREATE INDEX IF NOT EXISTS playlist_items_media_idx ON playlist_items(media_id);
        )SQL");
        auto has_column = [this](const std::string& table, const std::string& column) {
            for (const auto& row : query("PRAGMA table_info(" + table + ")")) {
                if (row.value("name", std::string{}) == column) return true;
            }
            return false;
        };
        if (!has_column("global_compressor_settings", "output_gain_db")) {
            execute("ALTER TABLE global_compressor_settings ADD COLUMN output_gain_db REAL NOT NULL DEFAULT 9");
        }
        if (!has_column("media_compressor_settings", "output_gain_db")) {
            execute("ALTER TABLE media_compressor_settings ADD COLUMN output_gain_db REAL NOT NULL DEFAULT 9");
        }
        if (!has_column("media", "file_device")) {
            execute("ALTER TABLE media ADD COLUMN file_device INTEGER");
        }
        if (!has_column("media", "file_inode")) {
            execute("ALTER TABLE media ADD COLUMN file_inode INTEGER");
        }
        execute("CREATE INDEX IF NOT EXISTS media_file_identity_idx ON media(library_id,file_device,file_inode)");
    }

    ~Database() { if (db_) sqlite3_close(db_); }
    Database(const Database&) = delete;
    Database& operator=(const Database&) = delete;

    json query(const std::string& sql, const json& parameters = json::array()) {
        std::lock_guard lock(mutex_);
        sqlite3_stmt* statement = prepare(sql);
        try {
            bind(statement, parameters);
            json rows = json::array();
            while (true) {
                int status = sqlite3_step(statement);
                if (status == SQLITE_DONE) break;
                if (status != SQLITE_ROW) throw_sql("Database query failed");
                json row = json::object();
                for (int i = 0; i < sqlite3_column_count(statement); ++i) {
                    std::string name = sqlite3_column_name(statement, i);
                    switch (sqlite3_column_type(statement, i)) {
                        case SQLITE_INTEGER: row[name] = sqlite3_column_int64(statement, i); break;
                        case SQLITE_FLOAT: row[name] = sqlite3_column_double(statement, i); break;
                        case SQLITE_TEXT: row[name] = reinterpret_cast<const char*>(sqlite3_column_text(statement, i)); break;
                        case SQLITE_BLOB: {
                            auto data = static_cast<const unsigned char*>(sqlite3_column_blob(statement, i));
                            row[name] = base64url(data, static_cast<std::size_t>(sqlite3_column_bytes(statement, i)));
                            break;
                        }
                        default: row[name] = nullptr;
                    }
                }
                rows.push_back(std::move(row));
            }
            sqlite3_finalize(statement);
            return rows;
        } catch (...) {
            sqlite3_finalize(statement);
            throw;
        }
    }

    json one(const std::string& sql, const json& parameters = json::array()) {
        auto rows = query(sql, parameters);
        return rows.empty() ? json(nullptr) : rows.front();
    }

    std::int64_t execute(const std::string& sql, const json& parameters = json::array()) {
        std::lock_guard lock(mutex_);
        sqlite3_stmt* statement = prepare(sql);
        try {
            bind(statement, parameters);
            int status = sqlite3_step(statement);
            if (status != SQLITE_DONE && status != SQLITE_ROW) throw_sql("Database update failed");
            sqlite3_finalize(statement);
            return sqlite3_changes(db_);
        } catch (...) {
            sqlite3_finalize(statement);
            throw;
        }
    }

    void transaction(const std::function<void(sqlite3*)>& operation) {
        std::lock_guard lock(mutex_);
        exec_unlocked("BEGIN IMMEDIATE");
        try {
            operation(db_);
            exec_unlocked("COMMIT");
        } catch (...) {
            try { exec_unlocked("ROLLBACK"); } catch (...) {}
            throw;
        }
    }

    fs::path path() const { return path_; }

    static void bind_values(sqlite3* db, sqlite3_stmt* statement, const json& parameters) {
        for (std::size_t i = 0; i < parameters.size(); ++i) {
            const auto& value = parameters[i];
            int index = static_cast<int>(i + 1);
            int status = SQLITE_OK;
            if (value.is_null()) status = sqlite3_bind_null(statement, index);
            else if (value.is_boolean()) status = sqlite3_bind_int(statement, index, value.get<bool>() ? 1 : 0);
            else if (value.is_number_integer()) status = sqlite3_bind_int64(statement, index, value.get<std::int64_t>());
            else if (value.is_number_unsigned()) status = sqlite3_bind_int64(statement, index, static_cast<sqlite3_int64>(value.get<std::uint64_t>()));
            else if (value.is_number_float()) status = sqlite3_bind_double(statement, index, value.get<double>());
            else {
                auto text = value.get<std::string>();
                status = sqlite3_bind_text(statement, index, text.c_str(), static_cast<int>(text.size()), SQLITE_TRANSIENT);
            }
            if (status != SQLITE_OK) throw std::runtime_error("Could not bind database parameter: " + std::string(sqlite3_errmsg(db)));
        }
    }

private:
    fs::path path_;
    sqlite3* db_{};
    std::mutex mutex_;

    sqlite3_stmt* prepare(const std::string& sql) {
        sqlite3_stmt* statement{};
        if (sqlite3_prepare_v2(db_, sql.c_str(), static_cast<int>(sql.size()), &statement, nullptr) != SQLITE_OK) {
            throw_sql("Could not prepare database query");
        }
        return statement;
    }
    void bind(sqlite3_stmt* statement, const json& parameters) { bind_values(db_, statement, parameters); }
    [[noreturn]] void throw_sql(const std::string& message) { throw std::runtime_error(message + ": " + sqlite3_errmsg(db_)); }
    void exec_unlocked(const std::string& sql) {
        char* error{};
        if (sqlite3_exec(db_, sql.c_str(), nullptr, nullptr, &error) != SQLITE_OK) {
            std::string message = error ? error : sqlite3_errmsg(db_);
            sqlite3_free(error);
            throw std::runtime_error(message);
        }
    }
    void execute_script(const std::string& sql) { std::lock_guard lock(mutex_); exec_unlocked(sql); }
};

class AuthManager {
public:
    explicit AuthManager(fs::path data_dir) : path_(std::move(data_dir) / "auth.json") {}
    bool configured() const { return fs::is_regular_file(path_); }

    fs::path initialize() {
        if (configured()) throw std::runtime_error("Authentication is already configured in " + path_.string());
        auto password = random_token(24);
        set_password(password);
        auto initial = path_.parent_path() / "initial-password.txt";
        write_file(initial, "Fluxa temporary household password\n\n" + password +
                    "\n\nChange it with: ./build/fluxa-server --config fluxa.json auth-set-password\n");
        return initial;
    }

    bool verify_password(const std::string& password) const {
        auto config = load();
        auto salt = base64url_decode(config.at("salt").get<std::string>());
        auto expected = base64url_decode(config.at("password_hash").get<std::string>());
        std::vector<unsigned char> actual(expected.size());
        auto scrypt = config.at("scrypt");
        if (EVP_PBE_scrypt(password.data(), password.size(), salt.data(), salt.size(),
                           scrypt.at("n").get<std::uint64_t>(), scrypt.at("r").get<std::uint64_t>(),
                           scrypt.at("p").get<std::uint64_t>(), 64ULL * 1024 * 1024,
                           actual.data(), actual.size()) != 1) {
            return false;
        }
        return actual.size() == expected.size() && CRYPTO_memcmp(actual.data(), expected.data(), actual.size()) == 0;
    }

    std::string issue_session(std::int64_t ttl_seconds = kSessionSeconds) const {
        auto config = load();
        auto secret = base64url_decode(config.at("session_secret").get<std::string>());
        auto expires = static_cast<std::int64_t>(std::time(nullptr)) + ttl_seconds;
        std::string payload = std::to_string(expires) + ":" + random_token();
        unsigned int length{};
        unsigned char digest[EVP_MAX_MD_SIZE];
        HMAC(EVP_sha256(), secret.data(), static_cast<int>(secret.size()),
             reinterpret_cast<const unsigned char*>(payload.data()), payload.size(), digest, &length);
        return base64url(reinterpret_cast<const unsigned char*>(payload.data()), payload.size()) + "." + base64url(digest, length);
    }

    bool verify_session(const std::string& token) const {
        try {
            auto dot = token.find('.');
            if (dot == std::string::npos) return false;
            auto payload_bytes = base64url_decode(token.substr(0, dot));
            auto supplied = base64url_decode(token.substr(dot + 1));
            std::string payload(payload_bytes.begin(), payload_bytes.end());
            auto colon = payload.find(':');
            if (colon == std::string::npos || std::stoll(payload.substr(0, colon)) < std::time(nullptr)) return false;
            auto config = load();
            auto secret = base64url_decode(config.at("session_secret").get<std::string>());
            unsigned int length{};
            unsigned char digest[EVP_MAX_MD_SIZE];
            HMAC(EVP_sha256(), secret.data(), static_cast<int>(secret.size()),
                 reinterpret_cast<const unsigned char*>(payload.data()), payload.size(), digest, &length);
            return supplied.size() == length && CRYPTO_memcmp(supplied.data(), digest, length) == 0;
        } catch (...) { return false; }
    }

    void set_password(const std::string& password, bool preserve_sessions = false) {
        if (password.size() < 12) throw std::runtime_error("The household password must contain at least 12 characters");
        std::vector<unsigned char> salt(16), digest(32), secret(32);
        if (RAND_bytes(salt.data(), salt.size()) != 1) {
            throw std::runtime_error("Could not create authentication secrets");
        }
        if (preserve_sessions) {
            auto existing = load();
            secret = base64url_decode(existing.at("session_secret").get<std::string>());
            if (secret.empty()) throw std::runtime_error("The existing session secret is invalid");
        } else if (RAND_bytes(secret.data(), secret.size()) != 1) {
            throw std::runtime_error("Could not create authentication secrets");
        }
        if (EVP_PBE_scrypt(password.data(), password.size(), salt.data(), salt.size(), 1ULL << 15, 8, 1,
                           64ULL * 1024 * 1024, digest.data(), digest.size()) != 1) {
            throw std::runtime_error("Could not hash household password");
        }
        json payload = {
            {"version", 1}, {"created_at", iso_timestamp()},
            {"scrypt", {{"n", 1ULL << 15}, {"r", 8}, {"p", 1}}},
            {"salt", base64url(salt.data(), salt.size())},
            {"password_hash", base64url(digest.data(), digest.size())},
            {"session_secret", base64url(secret.data(), secret.size())}
        };
        write_file(path_, payload.dump(2) + "\n");
    }

    fs::path path() const { return path_; }

private:
    fs::path path_;
    json load() const {
        auto value = json::parse(read_file(path_));
        if (value.value("version", 0) != 1) throw std::runtime_error("Unsupported authentication version");
        return value;
    }
};

json parse_json_field(const json& row, const char* key, json fallback = json::array()) {
    auto found = row.find(key);
    if (found == row.end() || found->is_null()) return fallback;
    try {
        if (found->is_string()) return json::parse(found->get<std::string>());
        return *found;
    } catch (...) { return fallback; }
}

template <typename T>
T number_or(const json& row, const char* key, T fallback = T{}) {
    auto found = row.find(key);
    if (found == row.end() || found->is_null()) return fallback;
    try { return found->get<T>(); } catch (...) { return fallback; }
}

std::optional<std::string> string_optional(const json& row, const char* key) {
    auto found = row.find(key);
    if (found == row.end() || found->is_null()) return std::nullopt;
    return found->get<std::string>();
}

std::string string_or(const json& row, const char* key, std::string fallback = {}) {
    auto value = string_optional(row, key);
    return value ? *value : std::move(fallback);
}

double bounded_number(const json& payload, const char* key, double minimum, double maximum, double fallback) {
    auto found = payload.find(key);
    if (found == payload.end() || !found->is_number()) return fallback;
    double value = found->get<double>();
    if (!std::isfinite(value)) return fallback;
    return std::clamp(value, minimum, maximum);
}

const std::unordered_set<std::string> kDirectPlayExtensions = {
    ".aac", ".flac", ".m4a", ".mp3", ".mp4", ".ogg", ".opus", ".wav", ".webm"
};

const std::unordered_set<std::string> kVideoExtensions = {
    ".avi", ".m2ts", ".m4v", ".mkv", ".mov", ".mp4", ".mpeg", ".mpg", ".ts", ".webm"
};

const std::unordered_set<std::string> kAudioExtensions = {
    ".aac", ".aif", ".aiff", ".ape", ".flac", ".m4a", ".mp3", ".ogg", ".opus", ".wav", ".wma"
};

std::string mime_type(const fs::path& path) {
    static const std::unordered_map<std::string, std::string> types = {
        {".aac", "audio/aac"}, {".flac", "audio/flac"}, {".m4a", "audio/mp4"},
        {".mkv", "video/x-matroska"}, {".mp3", "audio/mpeg"}, {".mp4", "video/mp4"},
        {".ogg", "audio/ogg"}, {".opus", "audio/ogg"}, {".webm", "video/webm"},
        {".wav", "audio/wav"}, {".m3u8", "application/vnd.apple.mpegurl"},
        {".ts", "video/mp2t"}, {".m4s", "video/mp4"}, {".vtt", "text/vtt; charset=utf-8"},
        {".jpg", "image/jpeg"}, {".png", "image/png"}, {".ico", "image/x-icon"},
        {".svg", "image/svg+xml"}, {".webmanifest", "application/manifest+json; charset=utf-8"},
        {".js", "text/javascript; charset=utf-8"},
        {".css", "text/css; charset=utf-8"}, {".html", "text/html; charset=utf-8"}
    };
    auto extension = lower(path.extension().string());
    auto found = types.find(extension);
    return found == types.end() ? "application/octet-stream" : found->second;
}

struct CompatibilitySession {
    std::string id;
    int media_id{};
    fs::path directory;
    fs::path log_path;
    pid_t process{};
    std::int64_t start_ms{};
    std::int64_t duration_ms{};
    std::string video_mode;
    std::string segment_format;
    std::optional<int> subtitle_ordinal;
    std::string leveling_mode;
    bool paused{};
    std::chrono::steady_clock::time_point last_access;
};

class CompatibilityManager {
public:
    explicit CompatibilityManager(const fs::path& data_dir)
        : root_(data_dir / "streams"), log_root_(data_dir / "logs" / "transcodes") {
        fs::create_directories(root_);
        fs::create_directories(log_root_);
        remove_stale_directories();
        prune_logs();
        cleanup_thread_ = std::jthread([this](std::stop_token stop) { cleanup(stop); });
    }

    ~CompatibilityManager() {
        cleanup_thread_.request_stop();
        std::vector<std::string> ids;
        {
            std::lock_guard lock(mutex_);
            for (const auto& [id, ignored] : sessions_) { (void)ignored; ids.push_back(id); }
        }
        for (const auto& id : ids) close(id);
    }

    struct StartOptions {
        int media_id{};
        fs::path source;
        std::int64_t start_ms{};
        std::int64_t duration_ms{};
        int width{};
        int height{};
        std::string analysis_status;
        json envelope;
        std::string segment_format;
        std::optional<int> subtitle_ordinal;
        std::string subtitle_kind;
        json compressor;
        json bass;
    };

    std::shared_ptr<CompatibilitySession> start(StartOptions options) {
        if (options.duration_ms && options.start_ms >= options.duration_ms - 1000) options.start_ms = 0;
        options.start_ms = std::max<std::int64_t>(0, options.start_ms);
        options.segment_format = options.segment_format == "fmp4" ? "fmp4" : "mpegts";
        auto session = std::make_shared<CompatibilitySession>();
        session->id = random_token();
        session->media_id = options.media_id;
        session->directory = root_ / session->id;
        session->start_ms = options.start_ms;
        session->duration_ms = options.duration_ms;
        session->segment_format = options.segment_format;
        session->subtitle_ordinal = options.subtitle_ordinal;
        session->last_access = std::chrono::steady_clock::now();
        fs::create_directory(session->directory);
        ::chmod(session->directory.c_str(), 0700);

        auto leveler = write_leveler(*session, options.analysis_status, options.envelope);
        session->leveling_mode = leveler ? "mapped" : "live";
        std::vector<std::string> errors;
        for (const auto& [encoder, label] : std::vector<std::pair<std::string, std::string>>{
                 {"h264_nvenc", "GPU H.264 + AAC"}, {"libx264", "software H.264 + AAC"}}) {
            clear_outputs(session->directory);
            session->log_path = log_root_ / (iso_timestamp_filename() + "-media-" + std::to_string(options.media_id) +
                                              "-" + session->id.substr(0, 8) + "-" + encoder + ".log");
            auto command = transcode_command(options, session->directory, encoder, leveler);
            session->process = spawn_ffmpeg(command, session->log_path, *session);
            if (wait_until_ready(*session, 15s)) {
                session->video_mode = label;
                std::lock_guard lock(mutex_);
                sessions_[session->id] = session;
                return session;
            }
            terminate(*session);
            errors.push_back(encoder + ": " + log_tail(session->log_path));
        }
        std::error_code error;
        fs::remove_all(session->directory, error);
        std::string detail;
        for (const auto& item : errors) detail += (detail.empty() ? "" : "; ") + item;
        throw ApiError(422, "Could not start compatibility stream: " + detail);
    }

    fs::path get_file(const std::string& id, const std::string& name) {
        auto session = find(id);
        bool valid = name == "index.m3u8" || (session->segment_format == "fmp4" && name == "init.mp4");
        static const std::regex segment(R"(^segment-\d{6}\.(?:ts|m4s)$)");
        valid = valid || std::regex_match(name, segment);
        if (!valid) throw ApiError(404, "Compatibility stream file not found");
        auto path = session->directory / name;
        if (!fs::is_regular_file(path)) throw ApiError(404, "Compatibility stream segment is not ready");
        {
            std::lock_guard lock(mutex_);
            session->last_access = std::chrono::steady_clock::now();
        }
        return path;
    }

    json control(const std::string& id, std::string action) {
        action = lower(std::move(action));
        if (action == "close") {
            close(id);
            return {{"status", "closed"}, {"paused", false}};
        }
        auto session = find(id);
        {
            std::lock_guard lock(mutex_);
            session->last_access = std::chrono::steady_clock::now();
            if (action == "pause" && !session->paused) {
                if (::kill(-session->process, SIGSTOP) == 0) session->paused = true;
            } else if (action == "resume" && session->paused) {
                if (::kill(-session->process, SIGCONT) == 0) session->paused = false;
            } else if (action != "heartbeat" && action != "pause" && action != "resume") {
                throw ApiError(400, "Unknown compatibility stream control");
            }
        }
        return {{"status", "active"}, {"paused", session->paused}};
    }

    void close(const std::string& id) {
        std::shared_ptr<CompatibilitySession> session;
        {
            std::lock_guard lock(mutex_);
            auto found = sessions_.find(id);
            if (found == sessions_.end()) return;
            session = found->second;
            sessions_.erase(found);
        }
        terminate(*session);
        std::error_code error;
        fs::remove_all(session->directory, error);
    }

    std::size_t active_count() const {
        std::lock_guard lock(mutex_);
        return sessions_.size();
    }

    std::uintmax_t temporary_bytes() const {
        std::vector<fs::path> directories;
        {
            std::lock_guard lock(mutex_);
            for (const auto& [id, session] : sessions_) { (void)id; directories.push_back(session->directory); }
        }
        std::uintmax_t total = 0;
        for (const auto& directory : directories) {
            std::error_code error;
            for (fs::directory_iterator iterator(directory, error); !error && iterator != fs::directory_iterator(); iterator.increment(error)) {
                if (iterator->is_regular_file(error)) total += iterator->file_size(error);
            }
        }
        return total;
    }

private:
    fs::path root_;
    fs::path log_root_;
    mutable std::mutex mutex_;
    std::unordered_map<std::string, std::shared_ptr<CompatibilitySession>> sessions_;
    std::jthread cleanup_thread_;

    static std::string iso_timestamp_filename() {
        auto result = iso_timestamp();
        result.erase(std::remove(result.begin(), result.end(), '-'), result.end());
        result.erase(std::remove(result.begin(), result.end(), ':'), result.end());
        return result;
    }

    std::shared_ptr<CompatibilitySession> find(const std::string& id) {
        std::lock_guard lock(mutex_);
        auto found = sessions_.find(id);
        if (found == sessions_.end()) throw ApiError(404, "Compatibility stream session not found");
        return found->second;
    }

    std::optional<fs::path> write_leveler(const CompatibilitySession& session, const std::string& status, const json& envelope) {
        if (status != "done" || !envelope.is_array() || envelope.empty()) return std::nullopt;
        bool reduction = false;
        double current_gain = 0;
        std::vector<std::pair<std::int64_t, double>> commands;
        try {
            for (const auto& point : envelope) {
                auto stamp = point.at(0).get<std::int64_t>();
                auto gain = point.at(1).get<double>();
                if (gain < -0.01) reduction = true;
                if (stamp <= session.start_ms) current_gain = gain;
                else commands.emplace_back(stamp - session.start_ms, gain);
            }
        } catch (...) { return std::nullopt; }
        if (!reduction) return std::nullopt;
        commands.insert(commands.begin(), {0, current_gain});
        auto path = session.directory / "leveler.txt";
        std::ofstream output(path);
        for (const auto& [stamp, gain] : commands) {
            output << std::fixed << std::setprecision(3) << stamp / 1000.0
                   << " volume@leveler volume " << std::setprecision(8) << std::pow(10.0, gain / 20.0) << ";\n";
        }
        output.close();
        ::chmod(path.c_str(), 0600);
        return path;
    }

    static std::tuple<std::string, std::string, std::string> video_rates(int width, int height) {
        auto pixels = static_cast<std::int64_t>(width) * height;
        if (pixels <= 720 * 576) return {"1600k", "2400k", "4800k"};
        if (pixels <= 1280 * 720) return {"3200k", "4800k", "9600k"};
        if (pixels <= 1920 * 1080) return {"6000k", "8000k", "16000k"};
        return {"10000k", "14000k", "28000k"};
    }

    static std::vector<std::string> transcode_command(const StartOptions& options, const fs::path& directory,
                                                       const std::string& encoder, const std::optional<fs::path>& leveler) {
        auto [bitrate, maximum, buffer] = video_rates(options.width, options.height);
        const std::string video_filters = "bwdif=mode=send_frame:parity=auto:deint=interlaced,"
            "scale=w='min(1920,iw)':h=-2:force_original_aspect_ratio=decrease,format=yuv420p";
        std::vector<std::string> audio_filters;
        if (leveler) {
            audio_filters.push_back("asendcmd=f=" + leveler->string());
            audio_filters.push_back("volume@leveler=1.0:precision=float");
        } else {
            audio_filters.push_back("dynaudnorm=f=500:g=31:p=0.90:m=1.0:r=0.10:n=true:b=true");
        }
        auto compressor = options.compressor;
        if (compressor.value("enabled", true)) {
            double threshold = std::pow(10.0, compressor.value("threshold_db", -24.0) / 20.0);
            std::ostringstream filter;
            filter << std::fixed << std::setprecision(8) << "acompressor=threshold=" << threshold
                   << std::setprecision(2) << ":ratio=" << compressor.value("ratio", 8.0)
                   << ":attack=" << compressor.value("attack_ms", 15.0)
                   << ":release=" << compressor.value("release_ms", 750.0)
                   << ":makeup=1:knee=" << compressor.value("knee", 4.0)
                   << ":detection=rms:link=maximum";
            audio_filters.push_back(filter.str());
            std::ostringstream gain;
            gain << std::fixed << std::setprecision(2)
                 << "volume=" << compressor.value("output_gain_db", 9.0) << "dB:precision=float";
            audio_filters.push_back(gain.str());
        }
        if (options.bass.value("enabled", false)) {
            std::ostringstream bass;
            bass << std::fixed << std::setprecision(2)
                 << "bass=g=" << options.bass.value("gain_db", 4.0)
                 << ":f=95:w=0.70:t=q:precision=f32";
            audio_filters.push_back(bass.str());
        }
        double ceiling = std::pow(10.0, compressor.value("ceiling_db", -3.0) / 20.0);
        std::ostringstream limiter;
        limiter << std::fixed << std::setprecision(8) << "alimiter=limit=" << ceiling << ":attack=20:release=250:level=false";
        audio_filters.push_back(limiter.str());
        std::string audio_filter;
        for (const auto& part : audio_filters) audio_filter += (audio_filter.empty() ? "" : ",") + part;

        std::vector<std::string> command = {
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "warning", "-nostats",
            "-stats_period", "5", "-progress", "pipe:2", "-ss",
            [&] { std::ostringstream s; s << std::fixed << std::setprecision(3) << options.start_ms / 1000.0; return s.str(); }(),
            "-readrate", "1.0", "-readrate_initial_burst", "24", "-i", options.source.string()
        };
        if (options.subtitle_kind == "bitmap" && options.subtitle_ordinal) {
            std::string complex = "[0:v:0]" + video_filters + "[base];[0:s:" + std::to_string(*options.subtitle_ordinal) +
                "]scale=w='min(1920,iw)':h=-2:force_original_aspect_ratio=decrease,format=rgba[sub];"
                "[base][sub]overlay=eof_action=pass:shortest=0,format=yuv420p[outv]";
            command.insert(command.end(), {"-filter_complex", complex, "-map", "[outv]"});
        } else {
            command.insert(command.end(), {"-map", "0:v:0", "-vf", video_filters});
        }
        command.insert(command.end(), {"-map", "0:a:0?", "-c:v", encoder});
        if (encoder == "h264_nvenc") {
            command.insert(command.end(), {"-forced-idr", "1", "-preset", "p4", "-tune", "hq", "-profile:v", "high",
                                            "-rc", "vbr", "-cq", "22", "-b:v", bitrate, "-maxrate", maximum, "-bufsize", buffer});
        } else {
            command.insert(command.end(), {"-preset", "veryfast", "-profile:v", "high", "-crf", "21",
                                            "-maxrate", maximum, "-bufsize", buffer});
        }
        command.insert(command.end(), {"-force_key_frames", "expr:gte(t,n_forced*4)", "-c:a", "aac", "-b:a", "192k",
                                        "-ac", "2", "-ar", "48000", "-af", audio_filter, "-avoid_negative_ts", "make_zero",
                                        "-max_interleave_delta", "0", "-f", "hls", "-hls_segment_type", options.segment_format,
                                        "-hls_time", "4", "-hls_list_size", "24", "-hls_delete_threshold", "6",
                                        "-hls_flags", "delete_segments+independent_segments+temp_file"});
        if (options.segment_format == "fmp4") {
            command.insert(command.end(), {"-hls_fmp4_init_filename", "init.mp4", "-hls_segment_filename",
                                            (directory / "segment-%06d.m4s").string()});
        } else {
            command.insert(command.end(), {"-hls_segment_filename", (directory / "segment-%06d.ts").string()});
        }
        command.push_back((directory / "index.m3u8").string());
        return command;
    }

    static pid_t spawn_ffmpeg(const std::vector<std::string>& command, const fs::path& log_path,
                              const CompatibilitySession& session) {
        int log = ::open(log_path.c_str(), O_CREAT | O_WRONLY | O_TRUNC, 0600);
        if (log < 0) throw ApiError(500, "Could not create transcode log");
        std::string header = "Fluxa C++ session=" + session.id + " media=" + std::to_string(session.media_id) +
            " start_ms=" + std::to_string(session.start_ms) + "\ncommand=" + shell_display(command) + "\n";
        auto ignored_write = ::write(log, header.data(), header.size());
        (void)ignored_write;
        pid_t pid = ::fork();
        if (pid < 0) { ::close(log); throw ApiError(500, "Could not fork FFmpeg"); }
        if (pid == 0) {
            ::setsid();
            int nullfd = ::open("/dev/null", O_RDWR);
            if (nullfd >= 0) { ::dup2(nullfd, STDIN_FILENO); ::dup2(nullfd, STDOUT_FILENO); }
            ::dup2(log, STDERR_FILENO);
            ::close(log);
            std::vector<char*> argv;
            for (const auto& part : command) argv.push_back(const_cast<char*>(part.c_str()));
            argv.push_back(nullptr);
            ::execvp(argv[0], argv.data());
            _exit(127);
        }
        ::close(log);
        return pid;
    }

    static bool wait_until_ready(CompatibilitySession& session, std::chrono::seconds timeout) {
        auto deadline = std::chrono::steady_clock::now() + timeout;
        auto playlist = session.directory / "index.m3u8";
        while (std::chrono::steady_clock::now() < deadline) {
            if (fs::is_regular_file(playlist)) {
                try {
                    auto content = read_file(playlist);
                    std::size_t count = 0, offset = 0;
                    while ((offset = content.find("#EXTINF:", offset)) != std::string::npos) { ++count; offset += 8; }
                    // Firefox/Android will accept a two-segment live playlist,
                    // but can sit at 0.2 seconds until more future data arrives.
                    // FFmpeg already has a 24-second startup burst, so expose
                    // four segments before handing the session to the player.
                    if (count >= 4 || (count >= 1 && content.find("#EXT-X-ENDLIST") != std::string::npos)) return true;
                } catch (...) {}
            }
            int status{};
            if (::waitpid(session.process, &status, WNOHANG) == session.process) { session.process = 0; return false; }
            std::this_thread::sleep_for(100ms);
        }
        return false;
    }

    static void terminate(CompatibilitySession& session) {
        if (session.process <= 0) return;
        ::kill(-session.process, SIGCONT);
        ::kill(-session.process, SIGTERM);
        auto deadline = std::chrono::steady_clock::now() + 2s;
        int status{};
        while (std::chrono::steady_clock::now() < deadline) {
            auto result = ::waitpid(session.process, &status, WNOHANG);
            if (result == session.process || (result < 0 && errno == ECHILD)) { session.process = 0; return; }
            std::this_thread::sleep_for(50ms);
        }
        ::kill(-session.process, SIGKILL);
        while (::waitpid(session.process, &status, 0) < 0 && errno == EINTR) {}
        session.process = 0;
    }

    static std::string log_tail(const fs::path& path) {
        try {
            auto text = trim(read_file(path));
            if (text.size() > 800) text.erase(0, text.size() - 800);
            return text.empty() ? "FFmpeg exited before creating a stream" : text;
        } catch (...) { return "FFmpeg exited before creating a stream"; }
    }

    static void clear_outputs(const fs::path& directory) {
        std::error_code error;
        for (fs::directory_iterator iterator(directory, error); !error && iterator != fs::directory_iterator(); iterator.increment(error)) {
            auto name = iterator->path().filename().string();
            if (name.starts_with("index.m3u8") || name.starts_with("segment-") || name == "init.mp4") fs::remove(iterator->path(), error);
        }
    }

    void cleanup(std::stop_token stop) {
        while (!stop.stop_requested()) {
            for (int i = 0; i < 150 && !stop.stop_requested(); ++i) std::this_thread::sleep_for(100ms);
            if (stop.stop_requested()) break;
            auto cutoff = std::chrono::steady_clock::now() - 75s;
            std::vector<std::string> expired;
            {
                std::lock_guard lock(mutex_);
                for (const auto& [id, session] : sessions_) if (session->last_access < cutoff) expired.push_back(id);
            }
            for (const auto& id : expired) close(id);
        }
    }

    void remove_stale_directories() {
        std::error_code error;
        for (fs::directory_iterator iterator(root_, error); !error && iterator != fs::directory_iterator(); iterator.increment(error)) {
            if (iterator->is_directory(error)) fs::remove_all(iterator->path(), error);
        }
    }

    void prune_logs() {
        std::vector<fs::directory_entry> logs;
        std::error_code error;
        for (fs::directory_iterator iterator(log_root_, error); !error && iterator != fs::directory_iterator(); iterator.increment(error)) {
            if (iterator->is_regular_file(error) && iterator->path().extension() == ".log") logs.push_back(*iterator);
        }
        std::sort(logs.begin(), logs.end(), [](const auto& left, const auto& right) {
            std::error_code e1, e2;
            return left.last_write_time(e1) > right.last_write_time(e2);
        });
        for (std::size_t i = 100; i < logs.size(); ++i) fs::remove(logs[i].path(), error);
    }
};

json compressor_from_row(const json& row) {
    return {
        {"enabled", number_or<std::int64_t>(row, "enabled") != 0},
        {"threshold_db", number_or<double>(row, "threshold_db", -24)},
        {"ratio", number_or<double>(row, "ratio", 8)},
        {"output_gain_db", number_or<double>(row, "output_gain_db", 9)},
        {"ceiling_db", number_or<double>(row, "ceiling_db", -3)},
        {"attack_ms", number_or<double>(row, "attack_ms", 15)},
        {"release_ms", number_or<double>(row, "release_ms", 750)},
        {"knee", number_or<double>(row, "knee", 4)}
    };
}

json normalize_compressor(const json& payload, const json& fallback) {
    bool enabled = fallback.value("enabled", true);
    if (payload.contains("enabled") && payload["enabled"].is_boolean()) enabled = payload["enabled"].get<bool>();
    return {
        {"enabled", enabled},
        {"threshold_db", bounded_number(payload, "threshold_db", -36, -8, fallback.value("threshold_db", -24.0))},
        {"ratio", bounded_number(payload, "ratio", 2, 16, fallback.value("ratio", 8.0))},
        {"output_gain_db", bounded_number(payload, "output_gain_db", -12, 18, fallback.value("output_gain_db", 9.0))},
        {"ceiling_db", bounded_number(payload, "ceiling_db", -12, -1, fallback.value("ceiling_db", -3.0))},
        {"attack_ms", bounded_number(payload, "attack_ms", 1, 200, fallback.value("attack_ms", 15.0))},
        {"release_ms", bounded_number(payload, "release_ms", 50, 3000, fallback.value("release_ms", 750.0))},
        {"knee", bounded_number(payload, "knee", 1, 8, fallback.value("knee", 4.0))}
    };
}

std::string escape_like(std::string value) {
    value = replace_all(std::move(value), "\\", "\\\\");
    value = replace_all(std::move(value), "%", "\\%");
    return replace_all(std::move(value), "_", "\\_");
}

std::optional<std::string> sanitize_folder(std::string value) {
    std::replace(value.begin(), value.end(), '\\', '/');
    while (!value.empty() && value.front() == '/') value.erase(value.begin());
    while (!value.empty() && value.back() == '/') value.pop_back();
    if (value.size() > 1024) return std::nullopt;
    if (value.empty()) return std::string{};
    std::string out;
    std::string part;
    std::stringstream parts(value);
    while (std::getline(parts, part, '/')) {
        if (part.empty() || part == "." || part == "..") return std::nullopt;
        if (!out.empty()) out += "/";
        out += part;
    }
    return out;
}

std::optional<std::int64_t> nonnegative_integer(const json& value) {
    try {
        if (value.is_number_integer()) {
            auto number = value.get<std::int64_t>();
            if (number >= 0) return number;
        }
    } catch (...) {}
    return std::nullopt;
}

std::optional<std::int64_t> query_integer(const httplib::Request& request, const char* key) {
    if (!request.has_param(key)) return std::nullopt;
    try { return std::stoll(request.get_param_value(key)); } catch (...) { return std::nullopt; }
}

std::string clean_title(std::string value) {
    std::replace(value.begin(), value.end(), '.', ' ');
    std::replace(value.begin(), value.end(), '_', ' ');
    value = std::regex_replace(value, std::regex(R"(\s+)"), " ");
    while (!value.empty() && (value.front() == ' ' || value.front() == '-')) value.erase(value.begin());
    while (!value.empty() && (value.back() == ' ' || value.back() == '-')) value.pop_back();
    return value;
}

std::string sort_key(std::string value) {
    value = lower(clean_title(std::move(value)));
    value = std::regex_replace(value, std::regex(R"(^(the|an|a)\s+)", std::regex::icase), "");
    return value;
}

struct EpisodeInfo {
    std::optional<std::string> show;
    std::optional<int> season;
    std::optional<int> episode;
    std::optional<std::string> title;
};

EpisodeInfo episode_info(const std::string& stem) {
    static const std::regex standard(R"((.*?)[ ._-]+s(\d{1,2})e(\d{1,3})(.*)$)", std::regex::icase);
    static const std::regex disc(R"(^\d{1,4}[ ._-]+s(\d{1,2})d\d{1,2}[ ._-]+(.*)$)", std::regex::icase);
    static const std::regex ordered(R"(^\d{3}[ ._-]+(.+)$)", std::regex::icase);
    std::smatch match;
    EpisodeInfo result;
    if (std::regex_match(stem, match, standard)) {
        auto show = clean_title(match[1]);
        if (!show.empty() && !std::all_of(show.begin(), show.end(), ::isdigit)) result.show = show;
        result.season = std::stoi(match[2]);
        result.episode = std::stoi(match[3]);
        auto title = clean_title(std::regex_replace(match[4].str(), std::regex(R"(^[ ._-]*(?:d\d{1,3})?[ ._-]*)", std::regex::icase), ""));
        if (!title.empty()) result.title = title;
    } else if (std::regex_match(stem, match, disc)) {
        result.season = std::stoi(match[1]);
        auto title = clean_title(match[2]);
        if (!title.empty()) result.title = title;
    } else if (std::regex_match(stem, match, ordered)) {
        auto title = clean_title(match[1]);
        if (!title.empty()) result.title = title;
    }
    return result;
}

class FluxaApp {
public:
    explicit FluxaApp(Config config)
        : config_(std::move(config)), database_(config_.database_path), auth_(config_.data_dir),
          compatibility_(config_.data_dir), public_dir_(config_.config_path.parent_path()),
          thumbnail_dir_(config_.data_dir / "thumbnails"), caption_dir_(config_.data_dir / "captions"),
          chapter_thumbnail_dir_(config_.data_dir / "chapter-thumbnails"),
          playback_log_(config_.data_dir / "logs" / "playback-events.jsonl") {
        fs::create_directories(thumbnail_dir_);
        fs::create_directories(caption_dir_);
        fs::create_directories(chapter_thumbnail_dir_);
        fs::create_directories(playback_log_.parent_path());
        sync_libraries();
        // FredPlayer owns music discovery, metadata, artwork, and playback.
        // Keep legacy audio rows only as inert history for old playlist links;
        // they are never exposed or probed by Fluxa.
        database_.execute("UPDATE media SET available=0 WHERE media_type='audio'");
        database_.execute("UPDATE media SET probe_status = 'pending' WHERE probe_status = 'probing'");
        database_.execute(R"SQL(UPDATE media SET probe_status = 'pending'
            WHERE available = 1 AND media_type = 'video' AND probe_status = 'done'
              AND (chapters_json IS NULL OR subtitle_streams_json IS NULL))SQL");
    }

    ~FluxaApp() {
        stopping_ = true;
        if (probe_thread_.joinable()) probe_thread_.request_stop();
        if (artwork_thread_.joinable()) artwork_thread_.request_stop();
    }

    const Config& config() const { return config_; }
    AuthManager& auth() { return auth_; }
    fs::path public_dir() const { return public_dir_; }

    json status() {
        auto totals = database_.one(R"SQL(
            SELECT COUNT(CASE WHEN m.available = 1 AND l.enabled = 1 AND m.media_type='video' THEN 1 END) AS total,
                   COUNT(CASE WHEN m.available = 1 AND l.enabled = 1 AND m.media_type='video' THEN 1 END) AS available,
                   COUNT(CASE WHEN m.available = 1 AND l.enabled = 1 AND m.media_type='video' THEN 1 END) AS videos,
                   0 AS audio,
                   COUNT(CASE WHEN m.probe_status = 'done' AND m.available = 1 AND l.enabled = 1 AND m.media_type='video' THEN 1 END) AS probed
            FROM media m JOIN libraries l ON l.id=m.library_id)SQL");
        std::error_code error;
        auto database_size = fs::is_regular_file(config_.database_path, error) ? fs::file_size(config_.database_path, error) : 0;
        return {
            {"name", "Fluxa"}, {"version", kVersion},
            {"runtime", "C++20"},
            {"media", {
                {"total", number_or<std::int64_t>(totals, "total")},
                {"available", number_or<std::int64_t>(totals, "available")},
                {"videos", number_or<std::int64_t>(totals, "videos")},
                {"audio", number_or<std::int64_t>(totals, "audio")},
                {"probed", number_or<std::int64_t>(totals, "probed")}
            }},
            {"scanner", {{"running", scan_running_.load()}}},
            {"probe", {{"running", probe_running_.load()}, {"completed_this_run", probe_completed_.load()}}},
            {"analysis", {{"running", analysis_jobs_json()}}},
            {"thumbnails", {
                {"pending", artwork_pending_.load()}, {"episode_pending", episode_pending_.load()},
                {"chapter_pending", chapter_pending_.load()}, {"backfill_running", artwork_running_.load()},
                {"backfill_completed", artwork_completed_.load()},
                {"fredplayer_cache", config_.fredplayer_artwork.enabled}
            }},
            {"compatibility", {{"active_sessions", compatibility_.active_count()}, {"temporary_bytes", compatibility_.temporary_bytes()}}},
            {"tools", {{"ffmpeg", command_available("ffmpeg")}, {"ffprobe", command_available("ffprobe")}}},
            {"logs", {{"playback", playback_log_.string()}, {"transcodes", (config_.data_dir / "logs" / "transcodes").string()},
                       {"service", "journalctl --user -u fluxa"}}},
            {"storage", {{"database_bytes", database_size}, {"media_copied_bytes", 0}}}
        };
    }

    json libraries() {
        auto rows = database_.query(R"SQL(
            SELECT l.id, l.name, l.kind, l.enabled, l.last_scan_at, l.last_scan_error,
                   COUNT(CASE WHEN m.available = 1 THEN 1 END) AS item_count,
                   COALESCE(SUM(CASE WHEN m.available = 1 THEN m.size_bytes ELSE 0 END), 0) AS total_bytes
            FROM libraries l LEFT JOIN media m ON m.library_id = l.id
            WHERE l.enabled=1 AND l.kind='video'
            GROUP BY l.id ORDER BY l.kind DESC, l.name COLLATE NOCASE)SQL");
        json result = json::array();
        for (const auto& row : rows) {
            result.push_back({
                {"id", number_or<std::int64_t>(row, "id")}, {"name", string_or(row, "name")},
                {"kind", string_or(row, "kind")}, {"enabled", number_or<std::int64_t>(row, "enabled") != 0},
                {"item_count", number_or<std::int64_t>(row, "item_count")}, {"total_bytes", number_or<std::int64_t>(row, "total_bytes")},
                {"last_scan_at", row.value("last_scan_at", json(nullptr))}, {"last_scan_error", row.value("last_scan_error", json(nullptr))}
            });
        }
        return result;
    }

    json playlists() {
        auto rows = database_.query(R"SQL(
            SELECT pl.id, pl.title, pl.kind, pl.source, pl.imported_at, COUNT(pi.id) AS item_count,
                   COUNT(CASE WHEN m.available = 1 AND l.enabled = 1 THEN 1 END) AS available_count
            FROM playlists pl LEFT JOIN playlist_items pi ON pi.playlist_id = pl.id
            LEFT JOIN media m ON m.id = pi.media_id LEFT JOIN libraries l ON l.id = m.library_id
            WHERE pl.kind != 'audio'
            GROUP BY pl.id ORDER BY pl.sort_title COLLATE NOCASE, pl.id)SQL");
        json result = json::array();
        for (const auto& row : rows) {
            result.push_back({
                {"id", number_or<std::int64_t>(row, "id")}, {"title", string_or(row, "title")},
                {"kind", string_or(row, "kind")}, {"source", string_or(row, "source")},
                {"item_count", number_or<std::int64_t>(row, "item_count")},
                {"available_count", number_or<std::int64_t>(row, "available_count")},
                {"imported_at", row.value("imported_at", json(nullptr))}
            });
        }
        return result;
    }

    json playlist_detail(int playlist_id, const httplib::Request& request) {
        auto playlist = database_.one(R"SQL(SELECT id,title,kind,source,imported_at,source_item_count,matched_item_count
                                             FROM playlists WHERE id = ?)SQL", {playlist_id});
        if (playlist.is_null()) throw ApiError(404, "Playlist not found");
        std::string search = request.has_param("q") ? trim(request.get_param_value("q")) : "";
        std::string clause;
        json parameters = json::array({playlist_id});
        if (!search.empty()) {
            clause = " AND (pi.source_title LIKE ? ESCAPE '\\' OR COALESCE(m.title,'') LIKE ? ESCAPE '\\')";
            auto term = "%" + escape_like(search) + "%";
            parameters.push_back(term); parameters.push_back(term);
        }
        auto rows = database_.query(R"SQL(
            SELECT m.*, l.name AS library_name, l.enabled AS library_enabled,
                   progress.position_ms, progress.duration_ms AS progress_duration_ms, progress.completed,
                   analysis.status AS analysis_status, analysis.spike_segments_json,
                   pi.position AS playlist_position, pi.source_title, pi.source_path
            FROM playlist_items pi LEFT JOIN media m ON m.id = pi.media_id
            LEFT JOIN libraries l ON l.id = m.library_id
            LEFT JOIN playback_progress progress ON progress.media_id = m.id
            LEFT JOIN loudness_analyses analysis ON analysis.media_id = m.id
            WHERE pi.playlist_id = ?)SQL" + clause + " ORDER BY pi.position", parameters);
        json items = json::array();
        std::int64_t available = 0;
        for (const auto& row : rows) {
            bool playable = !row.at("id").is_null() && number_or<std::int64_t>(row, "available") && number_or<std::int64_t>(row, "library_enabled");
            json item;
            if (playable) {
                item = public_media(row, false);
                item["available"] = true;
                ++available;
            } else {
                fs::path source = string_or(row, "source_path");
                auto extension = lower(source.extension().string());
                item = {
                    {"id", nullptr}, {"available", false}, {"title", string_or(row, "source_title")},
                    {"file_name", source.filename().string()}, {"media_type", kAudioExtensions.contains(extension) ? "audio" : "video"},
                    {"extension", extension}, {"duration_ms", 0}, {"show_title", nullptr},
                    {"library_name", "Unavailable Plex item"},
                    {"progress", {{"position_ms", 0}, {"duration_ms", 0}, {"completed", false}}},
                    {"analysis", {{"status", "unavailable"}, {"spike_count", 0}}}
                };
            }
            item["playlist_position"] = number_or<std::int64_t>(row, "playlist_position");
            items.push_back(std::move(item));
        }
        return {
            {"playlist", {{"id", number_or<std::int64_t>(playlist, "id")}, {"title", string_or(playlist, "title")},
                           {"kind", string_or(playlist, "kind")}, {"source", string_or(playlist, "source")},
                           {"imported_at", playlist.value("imported_at", json(nullptr))}}},
            {"items", std::move(items)}, {"total", rows.size()}, {"available", available}
        };
    }

    json create_playlist(const json& body) {
        auto title = trim(body.value("title", ""));
        if (title.empty()) throw ApiError(400, "Playlist title is required");
        if (title.size() > 200) throw ApiError(400, "Playlist title is too long");
        std::string kind = body.value("kind", "video");
        if (kind != "video" && kind != "audio" && kind != "mixed") kind = "video";
        auto source_id = std::to_string(std::chrono::system_clock::now().time_since_epoch().count())
            + "-" + std::to_string(::getpid());
        database_.execute(
            "INSERT INTO playlists(source,source_id,title,sort_title,kind) VALUES('fluxa',?,?,?,?)",
            {source_id, title, sort_key(title), kind});
        auto row = database_.one("SELECT id FROM playlists WHERE source='fluxa' AND source_id=?", {source_id});
        return {{"playlist", {{"id", number_or<std::int64_t>(row, "id")}, {"title", title}, {"kind", kind},
                               {"source", "fluxa"}, {"item_count", 0}, {"available_count", 0}}},
                {"playlists", playlists()}};
    }

    json add_playlist_items(int playlist_id, const json& body);

    json media_list(const httplib::Request& request) {
        std::vector<std::string> clauses = {"m.available = 1", "m.media_type = 'video'", "l.enabled = 1"};
        json parameters = json::array();
        std::string search;
        if (request.has_param("q") && !trim(request.get_param_value("q")).empty()) {
            search = trim(request.get_param_value("q"));
            clauses.push_back("(m.title LIKE ? ESCAPE '\\' OR m.relative_path LIKE ? ESCAPE '\\')");
            auto term = "%" + escape_like(search) + "%";
            parameters.push_back(term); parameters.push_back(term);
        }
        if (request.has_param("type")) {
            auto type = request.get_param_value("type");
            if (type == "video" || type == "audio") { clauses.push_back("m.media_type = ?"); parameters.push_back(type); }
        }
        std::optional<int> library_id;
        if (auto library = query_integer(request, "library"); library && *library > 0) {
            library_id = static_cast<int>(*library);
            clauses.push_back("m.library_id = ?"); parameters.push_back(*library);
        }
        std::optional<std::string> folder;
        bool recursive = request.has_param("recursive") && request.get_param_value("recursive") == "1";
        if (library_id && request.has_param("folder")) {
            folder = sanitize_folder(request.get_param_value("folder"));
            if (!folder) throw ApiError(400, "Invalid folder path");
        }
        json folders = json::array();
        if (folder) {
            if (!folder->empty()) {
                clauses.push_back("m.relative_path LIKE ? ESCAPE '\\'");
                parameters.push_back(escape_like(*folder) + "/%");
            }
            if (!recursive && search.empty()) {
                json folder_params = parameters;
                std::string folder_sql;
                if (folder->empty()) {
                    folder_sql = R"SQL(
                        SELECT child AS name, COUNT(*) AS item_count FROM (
                            SELECT substr(m.relative_path, 1, instr(m.relative_path, '/') - 1) AS child
                            FROM media m JOIN libraries l ON l.id=m.library_id WHERE )SQL"
                        + [&]{ std::string where; for (const auto& clause : clauses) where += (where.empty() ? "" : " AND ") + clause; return where; }()
                        + " AND instr(m.relative_path, '/') > 0) GROUP BY child ORDER BY child COLLATE NOCASE";
                } else {
                    int rest_start = static_cast<int>(folder->size() + 2);
                    folder_sql = R"SQL(
                        SELECT child AS name, COUNT(*) AS item_count FROM (
                            SELECT substr(substr(m.relative_path, ?), 1, instr(substr(m.relative_path, ?), '/') - 1) AS child
                            FROM media m JOIN libraries l ON l.id=m.library_id WHERE )SQL"
                        + [&]{ std::string where; for (const auto& clause : clauses) where += (where.empty() ? "" : " AND ") + clause; return where; }()
                        + " AND instr(substr(m.relative_path, ?), '/') > 0) GROUP BY child ORDER BY child COLLATE NOCASE";
                    folder_params.insert(folder_params.begin(), rest_start);
                    folder_params.insert(folder_params.begin(), rest_start);
                    folder_params.push_back(rest_start);
                }
                for (const auto& row : database_.query(folder_sql, folder_params)) {
                    auto name = string_or(row, "name");
                    if (name.empty()) continue;
                    folders.push_back({
                        {"name", name},
                        {"path", folder->empty() ? name : *folder + "/" + name},
                        {"item_count", number_or<std::int64_t>(row, "item_count")}
                    });
                }
                if (folder->empty()) {
                    clauses.push_back("instr(m.relative_path, '/') = 0");
                } else {
                    clauses.push_back("instr(substr(m.relative_path, ?), '/') = 0");
                    parameters.push_back(static_cast<int>(folder->size() + 2));
                }
            }
        }
        auto limit_value = query_integer(request, "limit").value_or(120);
        auto offset_value = query_integer(request, "offset").value_or(0);
        int limit = static_cast<int>(std::clamp<std::int64_t>(limit_value, 1, 250));
        int offset = static_cast<int>(std::max<std::int64_t>(offset_value, 0));
        std::string where;
        for (const auto& clause : clauses) where += (where.empty() ? "" : " AND ") + clause;
        auto count = database_.one("SELECT COUNT(*) AS count FROM media m JOIN libraries l ON l.id=m.library_id WHERE " + where, parameters);
        auto row_parameters = parameters;
        row_parameters.push_back(limit); row_parameters.push_back(offset);
        auto rows = database_.query(R"SQL(
            SELECT m.*, l.name AS library_name, p.position_ms, p.duration_ms AS progress_duration_ms,
                   p.completed, a.status AS analysis_status, a.spike_segments_json
            FROM media m JOIN libraries l ON l.id=m.library_id
            LEFT JOIN playback_progress p ON p.media_id=m.id
            LEFT JOIN loudness_analyses a ON a.media_id=m.id WHERE )SQL" + where +
            " ORDER BY m.sort_title, m.id LIMIT ? OFFSET ?", row_parameters);
        json items = json::array();
        for (const auto& row : rows) items.push_back(public_media(row, false));
        json result = {{"items", std::move(items)}, {"total", number_or<std::int64_t>(count, "count")}, {"limit", limit}, {"offset", offset},
                       {"folders", std::move(folders)}};
        if (folder) result["folder"] = *folder;
        return result;
    }

    json media_detail(int media_id, bool ensure_probe = true) {
        auto row = media_row(media_id);
        if (row.is_null()) throw ApiError(404, "Media item not found");
        bool navigation_missing = string_or(row, "media_type") == "video" &&
            (row.at("chapters_json").is_null() || row.at("subtitle_streams_json").is_null());
        if (ensure_probe && (string_or(row, "probe_status") == "pending" || navigation_missing)) {
            try { probe_media(media_id); } catch (...) {}
            row = media_row(media_id);
        }
        auto item = public_media(row, true);
        item["compressor"] = compressor_settings(media_id);
        return item;
    }

    json compressor_settings(std::optional<int> media_id = std::nullopt) {
        auto global = compressor_from_row(database_.one("SELECT * FROM global_compressor_settings WHERE id=1"));
        json video = nullptr;
        if (media_id) {
            auto row = database_.one("SELECT * FROM media_compressor_settings WHERE media_id=?", {*media_id});
            if (!row.is_null()) video = compressor_from_row(row);
        }
        return {{"global", global}, {"video", video}, {"effective", video.is_null() ? global : video},
                {"source", video.is_null() ? "global" : "video"}};
    }

    json update_global_compressor(const json& payload) {
        auto settings = normalize_compressor(payload, compressor_settings()["global"]);
        database_.execute(R"SQL(UPDATE global_compressor_settings SET enabled=?,threshold_db=?,ratio=?,output_gain_db=?,ceiling_db=?,
            attack_ms=?,release_ms=?,knee=?,updated_at=CURRENT_TIMESTAMP WHERE id=1)SQL", compressor_values(settings));
        return compressor_settings();
    }

    json update_media_compressor(int media_id, const json& payload) {
        if (media_row(media_id).is_null()) throw ApiError(404, "Media item not found");
        if (payload.value("inherit_global", false)) {
            database_.execute("DELETE FROM media_compressor_settings WHERE media_id=?", {media_id});
            return compressor_settings(media_id);
        }
        auto settings = normalize_compressor(payload, compressor_settings(media_id)["effective"]);
        auto values = compressor_values(settings);
        values.insert(values.begin(), media_id);
        database_.execute(R"SQL(INSERT INTO media_compressor_settings(media_id,enabled,threshold_db,ratio,output_gain_db,ceiling_db,attack_ms,release_ms,knee)
            VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(media_id) DO UPDATE SET enabled=excluded.enabled,threshold_db=excluded.threshold_db,
            ratio=excluded.ratio,output_gain_db=excluded.output_gain_db,ceiling_db=excluded.ceiling_db,attack_ms=excluded.attack_ms,release_ms=excluded.release_ms,
            knee=excluded.knee,updated_at=CURRENT_TIMESTAMP)SQL", values);
        return compressor_settings(media_id);
    }

    json update_progress(int media_id, const json& payload) {
        if (media_row(media_id).is_null()) throw ApiError(404, "Media item not found");
        std::optional<std::int64_t> position;
        std::optional<std::int64_t> duration{0};
        if (payload.contains("position_ms")) position = nonnegative_integer(payload["position_ms"]);
        if (payload.contains("duration_ms")) duration = nonnegative_integer(payload["duration_ms"]);
        if (!position) throw ApiError(400, "position_ms must be a non-negative integer");
        if (!duration) throw ApiError(400, "duration_ms must be a non-negative integer");
        if (*duration && *position > *duration + 60000) throw ApiError(400, "position_ms is beyond the media duration");
        bool completed = payload.value("completed", false);
        database_.execute(R"SQL(INSERT INTO playback_progress(media_id,position_ms,duration_ms,completed) VALUES(?,?,?,?)
            ON CONFLICT(media_id) DO UPDATE SET position_ms=excluded.position_ms,duration_ms=excluded.duration_ms,
            completed=excluded.completed,updated_at=CURRENT_TIMESTAMP)SQL", {media_id, *position, *duration, completed});
        return {{"position_ms", *position}, {"duration_ms", *duration}, {"completed", completed}};
    }

    fs::path resolve_media_path(int media_id) {
        auto row = media_row(media_id);
        if (row.is_null()) throw ApiError(404, "Media item not found");
        std::error_code error;
        auto path = fs::canonical(string_or(row, "path"), error);
        if (error || !fs::is_regular_file(path)) throw ApiError(404, "Media file is unavailable");
        auto root = fs::canonical(string_or(row, "root_path"), error);
        if (error) throw ApiError(404, "Library folder is unavailable");
        auto comparison = std::mismatch(root.begin(), root.end(), path.begin(), path.end());
        if (comparison.first != root.end()) throw ApiError(403, "Media path is outside its configured library");
        return path;
    }

    json start_compatibility(int media_id, const json& payload) {
        auto row = media_row(media_id);
        if (row.is_null() || string_or(row, "media_type") != "video") throw ApiError(404, "Video item not found");
        std::int64_t start_ms = 0;
        if (payload.contains("start_ms")) {
            auto parsed = nonnegative_integer(payload["start_ms"]);
            if (!parsed) throw ApiError(400, "start_ms must be non-negative");
            start_ms = *parsed;
        }
        std::optional<int> subtitle;
        std::string subtitle_kind;
        if (payload.contains("subtitle_ordinal") && !payload["subtitle_ordinal"].is_null()) {
            auto parsed = nonnegative_integer(payload["subtitle_ordinal"]);
            if (!parsed) throw ApiError(400, "subtitle_ordinal must be non-negative");
            bool found = false;
            for (const auto& track : parse_json_field(row, "subtitle_streams_json")) {
                if (track.value("ordinal", -1) == *parsed) {
                    found = true;
                    subtitle_kind = track.value("kind", "unsupported");
                    if (subtitle_kind == "bitmap") subtitle = static_cast<int>(*parsed);
                    break;
                }
            }
            if (!found) throw ApiError(400, "Subtitle track not found");
        }
        auto compressor = compressor_settings(media_id)["effective"];
        json bass = {{"enabled", false}, {"gain_db", 4.0}};
        if (payload.contains("bass") && payload["bass"].is_object()) {
            const auto& requested = payload["bass"];
            bass["enabled"] = requested.value("enabled", false);
            bass["gain_db"] = bounded_number(requested, "gain_db", 0.0, 9.0, 4.0);
        }
        CompatibilityManager::StartOptions options;
        options.media_id = media_id;
        options.source = resolve_media_path(media_id);
        options.start_ms = start_ms;
        options.duration_ms = number_or<std::int64_t>(row, "duration_ms");
        options.width = number_or<int>(row, "width"); options.height = number_or<int>(row, "height");
        options.analysis_status = string_or(row, "analysis_status");
        options.envelope = parse_json_field(row, "envelope_json");
        options.segment_format = payload.value("segment_format", "mpegts") == "fmp4" ? "fmp4" : "mpegts";
        options.subtitle_ordinal = subtitle; options.subtitle_kind = subtitle_kind;
        options.compressor = compressor; options.bass = bass;
        auto session = compatibility_.start(std::move(options));
        return {
            {"session_id", session->id}, {"manifest_url", "/api/compat/" + session->id + "/index.m3u8"},
            {"start_ms", session->start_ms}, {"video_mode", session->video_mode}, {"audio_mode", "AAC stereo"},
            {"segment_format", session->segment_format}, {"leveling_mode", session->leveling_mode},
            {"leveling_applied", true}, {"compressor", compressor}, {"bass", bass},
            {"subtitle_ordinal", session->subtitle_ordinal ? json(*session->subtitle_ordinal) : json(nullptr)}
        };
    }

    json control_compatibility(const std::string& session, const json& payload) {
        if (!payload.contains("action") || !payload["action"].is_string()) throw ApiError(400, "A stream control action is required");
        return compatibility_.control(session, payload["action"].get<std::string>());
    }

    fs::path compatibility_file(const std::string& session, const std::string& file) { return compatibility_.get_file(session, file); }

    json record_playback_event(const json& payload, std::string user_agent) {
        json record = {{"timestamp", iso_timestamp()}, {"event", payload.value("event", "unknown")}, {"user_agent", user_agent.substr(0, 300)}};
        for (const auto* key : {"media_id", "position_ms", "buffered_ahead_ms", "stall_ms", "ready_state", "network_state",
                                "jump_ms", "elapsed_ms", "media_delta_ms", "expected_delta_ms"}) {
            if (payload.contains(key) && payload[key].is_number()) record[key] = payload[key];
        }
        for (const auto* key : {"session_id", "hls_type", "hls_detail", "hls_reason", "hls_buffer", "fullscreen_reason",
                                "fullscreen_target", "client_version"}) {
            if (payload.contains(key) && !payload[key].is_null()) record[key] = payload[key].dump().substr(0, 300);
        }
        for (const auto* key : {"paused", "hidden", "tizen_mode"}) if (payload.contains(key) && payload[key].is_boolean()) record[key] = payload[key];
        std::lock_guard lock(playback_log_mutex_);
        std::error_code error;
        if (fs::is_regular_file(playback_log_, error) && fs::file_size(playback_log_, error) >= 5 * 1024 * 1024) {
            fs::rename(playback_log_, playback_log_.string() + ".1", error);
        }
        std::ofstream output(playback_log_, std::ios::app);
        if (!output) throw ApiError(500, "Could not write playback diagnostics");
        output << record.dump() << '\n';
        return {{"recorded", true}, {"event", payload.value("event", "unknown")}};
    }

private:
    Config config_;
    Database database_;
    AuthManager auth_;
    CompatibilityManager compatibility_;
    fs::path public_dir_, thumbnail_dir_, caption_dir_, chapter_thumbnail_dir_, playback_log_;
    std::atomic<bool> stopping_{false}, scan_running_{false}, probe_running_{false}, artwork_running_{false};
    std::atomic<std::size_t> probe_completed_{0}, artwork_pending_{0}, episode_pending_{0}, chapter_pending_{0}, artwork_completed_{0};
    std::jthread probe_thread_, artwork_thread_;
    std::mutex scan_mutex_, artwork_mutex_, playback_log_mutex_, analysis_mutex_;
    std::unordered_set<int> thumbnail_generating_;
    std::set<std::pair<int, int>> chapter_generating_;
    std::set<int> analysis_jobs_;

    bool generate_thumbnail(int media_id);
    bool generate_chapter_thumbnail(int media_id, int chapter_index);
    void analyze_media(int media_id);
    fs::path thumbnail_path(const json& row) const;
    fs::path chapter_thumbnail_path(const json& row, int chapter_index) const;
    std::optional<std::string> fredplayer_relative_path(const json& row) const;

    static json compressor_values(const json& settings) {
        return json::array({settings.at("enabled"), settings.at("threshold_db"), settings.at("ratio"), settings.at("output_gain_db"), settings.at("ceiling_db"),
                            settings.at("attack_ms"), settings.at("release_ms"), settings.at("knee")});
    }

    static bool command_available(const char* command) {
        const char* path = std::getenv("PATH");
        if (!path) return false;
        std::stringstream paths(path);
        std::string directory;
        while (std::getline(paths, directory, ':')) if (::access((fs::path(directory) / command).c_str(), X_OK) == 0) return true;
        return false;
    }

    json analysis_jobs_json() {
        std::lock_guard lock(analysis_mutex_);
        return json(analysis_jobs_);
    }

    void sync_libraries() {
        for (const auto& library : config_.libraries) {
            database_.execute(R"SQL(INSERT INTO libraries(name,kind,root_path,enabled) VALUES(?,?,?,1)
                ON CONFLICT(root_path) DO UPDATE SET name=excluded.name,kind=excluded.kind,enabled=1)SQL",
                {library.name, library.kind, library.path.string()});
        }
        auto rows = database_.query("SELECT id,root_path FROM libraries");
        for (const auto& row : rows) {
            bool configured = std::any_of(config_.libraries.begin(), config_.libraries.end(), [&](const auto& library) {
                return library.path.string() == string_or(row, "root_path");
            });
            if (!configured) database_.execute("UPDATE libraries SET enabled=0 WHERE id=?", {number_or<std::int64_t>(row, "id")});
        }
    }

    json media_row(int media_id) {
        return database_.one(R"SQL(
            SELECT m.*, l.name AS library_name,l.root_path,
                   p.position_ms,p.duration_ms AS progress_duration_ms,p.completed,
                   a.status AS analysis_status,a.integrated_lufs,a.true_peak_db,a.sample_interval_ms,
                   a.envelope_json,a.spike_segments_json,a.error AS analysis_error,a.analyzed_at
            FROM media m JOIN libraries l ON l.id=m.library_id
            LEFT JOIN playback_progress p ON p.media_id=m.id
            LEFT JOIN loudness_analyses a ON a.media_id=m.id
            WHERE m.id=? AND m.available=1)SQL", {media_id});
    }

    json public_media(const json& row, bool detailed) {
        auto duration = number_or<std::int64_t>(row, "duration_ms", number_or<std::int64_t>(row, "progress_duration_ms"));
        auto position = number_or<std::int64_t>(row, "position_ms");
        auto media_id = number_or<std::int64_t>(row, "id");
        auto extension = string_or(row, "extension");
        auto spikes = parse_json_field(row, "spike_segments_json");
        auto fredplayer_path = fredplayer_relative_path(row);
        auto display_title = string_or(row, "title");
        if (auto file_title = episode_info(fs::path(string_or(row, "file_name")).stem().string()).title) {
            display_title = *file_title;
        }
        json item = {
            {"id", media_id}, {"available", true}, {"library_id", number_or<std::int64_t>(row, "library_id")},
            {"library_name", string_or(row, "library_name")}, {"title", display_title},
            {"file_name", string_or(row, "file_name")}, {"relative_path", string_or(row, "relative_path")},
            {"media_type", string_or(row, "media_type")}, {"extension", extension}, {"size_bytes", number_or<std::int64_t>(row, "size_bytes")},
            {"duration_ms", duration}, {"show_title", row.value("show_title", json(nullptr))},
            {"season_number", row.value("season_number", json(nullptr))}, {"episode_number", row.value("episode_number", json(nullptr))},
            {"probe_status", string_or(row, "probe_status")}, {"direct_play_likely", kDirectPlayExtensions.contains(extension)},
            {"stream_url", "/api/media/" + std::to_string(media_id) + "/stream"},
            {"fredplayer_path", fredplayer_path ? json(*fredplayer_path) : json(nullptr)},
            {"thumbnail_url", string_or(row, "media_type") == "video"
                ? json("/api/media/" + std::to_string(media_id) + "/thumbnail?v=2")
                : (fredplayer_path
                    ? json("/api/media/" + std::to_string(media_id) + "/thumbnail?v=fredplayer-1") : json(nullptr))},
            {"progress", {{"position_ms", position}, {"duration_ms", duration}, {"completed", number_or<std::int64_t>(row, "completed") != 0}}},
            {"analysis", {{"status", string_or(row, "analysis_status", "pending")}, {"spike_count", spikes.is_array() ? spikes.size() : 0}}}
        };
        if (detailed) {
            item["technical"] = {
                {"container", row.value("container", json(nullptr))}, {"video_codec", row.value("video_codec", json(nullptr))},
                {"width", row.value("width", json(nullptr))}, {"height", row.value("height", json(nullptr))},
                {"audio_codec", row.value("audio_codec", json(nullptr))}, {"audio_channels", row.value("audio_channels", json(nullptr))},
                {"audio_tracks", row.value("audio_tracks", json(nullptr))}, {"subtitle_tracks", row.value("subtitle_tracks", json(nullptr))},
                {"probe_error", row.value("probe_error", json(nullptr))}
            };
            auto chapters = parse_json_field(row, "chapters_json");
            for (std::size_t index = 0; index < chapters.size(); ++index) {
                chapters[index]["thumbnail_url"] = "/api/media/" + std::to_string(media_id) + "/chapters/" + std::to_string(index) + "/thumbnail?v=1";
            }
            auto subtitles = parse_json_field(row, "subtitle_streams_json");
            for (auto& track : subtitles) {
                if (track.value("kind", "") == "text") track["url"] = "/api/media/" + std::to_string(media_id) +
                    "/subtitles/" + std::to_string(track.value("ordinal", 0)) + ".vtt";
            }
            item["chapters"] = std::move(chapters);
            item["subtitles"] = std::move(subtitles);
            item["analysis"].update({
                {"integrated_lufs", row.value("integrated_lufs", json(nullptr))}, {"true_peak_db", row.value("true_peak_db", json(nullptr))},
                {"sample_interval_ms", row.value("sample_interval_ms", json(nullptr))}, {"segments", spikes},
                {"error", row.value("analysis_error", json(nullptr))}, {"analyzed_at", row.value("analyzed_at", json(nullptr))}
            });
        }
        return item;
    }

public:
    // Worker and file-serving operations are defined below to keep the API methods together.
    json scan();
    void start_probe_worker();
    void start_artwork_backfill();
    json probe_media(int media_id);
    bool start_analysis(int media_id);
    void analyze_now(int media_id) { analyze_media(media_id); }
    fs::path thumbnail(int media_id);
    std::optional<ArtworkResponse> fredplayer_artwork(int media_id);
    std::optional<ArtworkResponse> fredplayer_artwork_path(const std::string& relative_path);
    json fredplayer_library(const httplib::Request& request);
    json fredplayer_collections(const httplib::Request& request);
    bool redeem_fredplayer_grant(const std::string& grant);
    std::string fredplayer_launch_ticket(const json& request);
    fs::path chapter_thumbnail(int media_id, int chapter_index);
    fs::path caption(int media_id, int ordinal);
    json import_plex_playlists(const fs::path& plex_path);
};

void raw_execute(sqlite3* db, const std::string& sql, const json& parameters = json::array()) {
    sqlite3_stmt* statement{};
    if (sqlite3_prepare_v2(db, sql.c_str(), static_cast<int>(sql.size()), &statement, nullptr) != SQLITE_OK) {
        throw std::runtime_error("Could not prepare database update: " + std::string(sqlite3_errmsg(db)));
    }
    try {
        Database::bind_values(db, statement, parameters);
        int status = sqlite3_step(statement);
        if (status != SQLITE_DONE && status != SQLITE_ROW) throw std::runtime_error("Database update failed: " + std::string(sqlite3_errmsg(db)));
        sqlite3_finalize(statement);
    } catch (...) { sqlite3_finalize(statement); throw; }
}

json raw_one(sqlite3* db, const std::string& sql, const json& parameters = json::array()) {
    sqlite3_stmt* statement{};
    if (sqlite3_prepare_v2(db, sql.c_str(), static_cast<int>(sql.size()), &statement, nullptr) != SQLITE_OK) {
        throw std::runtime_error("Could not prepare database query: " + std::string(sqlite3_errmsg(db)));
    }
    try {
        Database::bind_values(db, statement, parameters);
        json row = nullptr;
        int status = sqlite3_step(statement);
        if (status == SQLITE_ROW) {
            row = json::object();
            for (int i = 0; i < sqlite3_column_count(statement); ++i) {
                std::string name = sqlite3_column_name(statement, i);
                switch (sqlite3_column_type(statement, i)) {
                    case SQLITE_INTEGER: row[name] = sqlite3_column_int64(statement, i); break;
                    case SQLITE_FLOAT: row[name] = sqlite3_column_double(statement, i); break;
                    case SQLITE_TEXT: row[name] = reinterpret_cast<const char*>(sqlite3_column_text(statement, i)); break;
                    default: row[name] = nullptr;
                }
            }
        } else if (status != SQLITE_DONE) throw std::runtime_error("Database query failed: " + std::string(sqlite3_errmsg(db)));
        sqlite3_finalize(statement);
        return row;
    } catch (...) { sqlite3_finalize(statement); throw; }
}

json raw_query(sqlite3* db, const std::string& sql, const json& parameters = json::array()) {
    sqlite3_stmt* statement{};
    if (sqlite3_prepare_v2(db, sql.c_str(), static_cast<int>(sql.size()), &statement, nullptr) != SQLITE_OK) {
        throw std::runtime_error("Could not prepare database query: " + std::string(sqlite3_errmsg(db)));
    }
    try {
        Database::bind_values(db, statement, parameters);
        json rows = json::array();
        while (true) {
            int status = sqlite3_step(statement);
            if (status == SQLITE_DONE) break;
            if (status != SQLITE_ROW) throw std::runtime_error("Database query failed: " + std::string(sqlite3_errmsg(db)));
            json row = json::object();
            for (int i = 0; i < sqlite3_column_count(statement); ++i) {
                std::string name = sqlite3_column_name(statement, i);
                switch (sqlite3_column_type(statement, i)) {
                    case SQLITE_INTEGER: row[name] = sqlite3_column_int64(statement, i); break;
                    case SQLITE_FLOAT: row[name] = sqlite3_column_double(statement, i); break;
                    case SQLITE_TEXT: row[name] = reinterpret_cast<const char*>(sqlite3_column_text(statement, i)); break;
                    default: row[name] = nullptr;
                }
            }
            rows.push_back(std::move(row));
        }
        sqlite3_finalize(statement);
        return rows;
    } catch (...) { sqlite3_finalize(statement); throw; }
}

json FluxaApp::add_playlist_items(int playlist_id, const json& body) {
    auto playlist = database_.one("SELECT id FROM playlists WHERE id=?", {playlist_id});
    if (playlist.is_null()) throw ApiError(404, "Playlist not found");
    if (!body.contains("media_ids") || !body["media_ids"].is_array()) {
        throw ApiError(400, "media_ids array is required");
    }
    if (body["media_ids"].size() > 500) throw ApiError(400, "At most 500 items can be added at once");
    int added = 0;
    int skipped = 0;
    database_.transaction([&](sqlite3* db) {
        auto next_row = raw_one(db, "SELECT COALESCE(MAX(position), -1) + 1 AS next FROM playlist_items WHERE playlist_id=?",
                                {playlist_id});
        int position = number_or<int>(next_row, "next");
        for (const auto& value : body["media_ids"]) {
            std::int64_t media_id = 0;
            if (value.is_number_integer()) media_id = value.get<std::int64_t>();
            else if (value.is_number_unsigned()) media_id = static_cast<std::int64_t>(value.get<std::uint64_t>());
            else { ++skipped; continue; }
            auto media = raw_one(db, R"SQL(
                SELECT m.id, m.title, m.path FROM media m
                JOIN libraries l ON l.id = m.library_id
                WHERE m.id=? AND m.available=1 AND m.media_type='video' AND l.enabled=1)SQL", {media_id});
            if (media.is_null()) { ++skipped; continue; }
            auto existing = raw_one(db, "SELECT id FROM playlist_items WHERE playlist_id=? AND media_id=?",
                                    {playlist_id, media_id});
            if (!existing.is_null()) { ++skipped; continue; }
            auto title = string_or(media, "title");
            auto path = string_or(media, "path");
            raw_execute(db, R"SQL(
                INSERT INTO playlist_items(playlist_id,position,media_id,source_item_id,source_path,source_title)
                VALUES(?,?,?,?,?,?))SQL",
                {playlist_id, position, media_id, "fluxa-" + std::to_string(media_id), path, title});
            ++position;
            ++added;
        }
        raw_execute(db, R"SQL(
            UPDATE playlists SET
                source_item_count=(SELECT COUNT(*) FROM playlist_items WHERE playlist_id=?),
                matched_item_count=(SELECT COUNT(*) FROM playlist_items WHERE playlist_id=? AND media_id IS NOT NULL),
                updated_at=CURRENT_TIMESTAMP
            WHERE id=?)SQL", {playlist_id, playlist_id, playlist_id});
    });
    return {{"added", added}, {"skipped", skipped}, {"playlists", playlists()}};
}

json FluxaApp::scan() {
    std::unique_lock scan_lock(scan_mutex_, std::try_to_lock);
    if (!scan_lock.owns_lock()) throw ApiError(409, "A library scan is already running");
    scan_running_ = true;
    struct Reset { std::atomic<bool>& flag; ~Reset() { flag = false; } } reset{scan_running_};
    sync_libraries();
    json results = json::array();
    for (const auto& library : config_.libraries) {
        auto started = std::chrono::steady_clock::now();
        std::string token = std::to_string(std::chrono::system_clock::now().time_since_epoch().count()) + "-" + std::to_string(::getpid());
        std::int64_t discovered = 0, added = 0, changed = 0, renamed = 0, skipped = 0, unavailable = 0;
        std::optional<std::string> scan_error;
        if (!fs::is_directory(library.path)) {
            scan_error = "Library folder is unavailable: " + library.path.string();
            database_.execute("UPDATE libraries SET last_scan_error=? WHERE root_path=?", {*scan_error, library.path.string()});
        } else {
            struct Entry {
                fs::path path; std::string relative, file_name, title, sort_title, media_type, extension;
                std::int64_t size{}, modified_ns{}, file_device{}, file_inode{}; std::optional<std::string> show;
                std::optional<int> season, episode; bool parsed_title{};
            };
            std::vector<Entry> entries;
            std::error_code error;
            fs::recursive_directory_iterator iterator(library.path, fs::directory_options::skip_permission_denied, error), end;
            while (iterator != end) {
                if (error) { ++skipped; error.clear(); iterator.increment(error); continue; }
                const auto path = iterator->path();
                auto name = lower(path.filename().string());
                if (iterator->is_directory(error)) {
                    static const std::unordered_set<std::string> ignored = {
                        "$recycle.bin", ".appledouble", ".snapshot", ".snapshots", "@eadir", "system volume information"
                    };
                    if ((!name.empty() && name.front() == '.') || ignored.contains(name)) iterator.disable_recursion_pending();
                    iterator.increment(error); continue;
                }
                if (!iterator->is_regular_file(error)) { iterator.increment(error); continue; }
                auto extension = lower(path.extension().string());
                std::string media_type;
                if (library.kind == "video" && kVideoExtensions.contains(extension)) media_type = "video";
                else if (library.kind == "music" && kAudioExtensions.contains(extension)) media_type = "audio";
                else { iterator.increment(error); continue; }
                if (iterator->is_symlink(error)) {
                    auto resolved = fs::canonical(path, error);
                    if (error || resolved.string().rfind(fs::canonical(library.path).string(), 0) != 0) {
                        ++skipped; iterator.increment(error); continue;
                    }
                }
                auto size = static_cast<std::int64_t>(iterator->file_size(error));
                auto modified = iterator->last_write_time(error).time_since_epoch().count();
                auto relative = fs::relative(path, library.path, error).string();
                if (error) { ++skipped; error.clear(); iterator.increment(error); continue; }
                struct stat file_status{};
                if (::stat(path.c_str(), &file_status) != 0) { ++skipped; iterator.increment(error); continue; }
                auto stem = path.stem().string();
                auto episode = episode_info(stem);
                auto title = episode.title.value_or(clean_title(stem));
                std::optional<std::string> show = episode.show;
                if (!show) {
                    auto parent = fs::path(relative).parent_path();
                    for (auto component = parent.end(); component != parent.begin();) {
                        --component;
                        auto candidate = clean_title(component->string());
                        auto folded = lower(candidate);
                        if (!candidate.empty() && folded != "files" && folded != "media" && folded != "episodes" &&
                            folded != "video" && folded != "videos" && !std::regex_match(folded, std::regex(R"(season\s*\d+)")) &&
                            !std::regex_match(folded, std::regex(R"((disc|disk)\s*\d+)"))) { show = candidate; break; }
                    }
                }
                entries.push_back({path, relative, path.filename().string(), title, sort_key(title), media_type, extension,
                                   size, static_cast<std::int64_t>(modified), static_cast<std::int64_t>(file_status.st_dev),
                                   static_cast<std::int64_t>(file_status.st_ino), show, episode.season, episode.episode,
                                   episode.title.has_value()});
                ++discovered;
                iterator.increment(error);
            }
            try {
                database_.transaction([&](sqlite3* db) {
                    auto library_row = raw_one(db, "SELECT id FROM libraries WHERE root_path=?", {library.path.string()});
                    if (library_row.is_null()) throw std::runtime_error("Library is not registered: " + library.path.string());
                    int library_id = number_or<int>(library_row, "id");
                    for (const auto& entry : entries) {
                        auto existing = raw_one(db, "SELECT id,size_bytes,modified_ns FROM media WHERE path=?", {entry.path.string()});
                        bool was_renamed = false;
                        if (existing.is_null()) {
                            auto missing_candidate = [](const json& candidates) {
                                json match = nullptr;
                                int count = 0;
                                for (const auto& candidate : candidates) {
                                    std::error_code candidate_error;
                                    if (!fs::is_regular_file(string_or(candidate, "path"), candidate_error)) {
                                        match = candidate;
                                        ++count;
                                    }
                                }
                                return count == 1 ? match : json(nullptr);
                            };
                            auto candidate = missing_candidate(raw_query(db, R"SQL(
                                SELECT id,path,size_bytes,modified_ns FROM media
                                WHERE library_id=? AND media_type=? AND file_device=? AND file_inode=? AND path!=?)SQL",
                                {library_id, entry.media_type, entry.file_device, entry.file_inode, entry.path.string()}));
                            if (candidate.is_null()) {
                                candidate = missing_candidate(raw_query(db, R"SQL(
                                    SELECT id,path,size_bytes,modified_ns FROM media
                                    WHERE library_id=? AND media_type=? AND size_bytes=? AND modified_ns=? AND path!=?)SQL",
                                    {library_id, entry.media_type, entry.size, entry.modified_ns, entry.path.string()}));
                            }
                            if (!candidate.is_null()) {
                                raw_execute(db, "UPDATE media SET path=? WHERE id=?",
                                            {entry.path.string(), number_or<std::int64_t>(candidate, "id")});
                                existing = candidate;
                                was_renamed = true;
                                ++renamed;
                            }
                        }
                        bool is_changed = !existing.is_null() &&
                            (number_or<std::int64_t>(existing, "size_bytes") != entry.size || number_or<std::int64_t>(existing, "modified_ns") != entry.modified_ns);
                        json values = {library_id, entry.path.string(), entry.relative, entry.file_name, entry.title, entry.sort_title,
                                       entry.media_type, entry.extension, entry.size, entry.modified_ns, entry.file_device, entry.file_inode, token,
                                       entry.show ? json(*entry.show) : json(nullptr), entry.season ? json(*entry.season) : json(nullptr),
                                       entry.episode ? json(*entry.episode) : json(nullptr), entry.parsed_title, entry.parsed_title};
                        raw_execute(db, R"SQL(INSERT INTO media(library_id,path,relative_path,file_name,title,sort_title,media_type,extension,
                            size_bytes,modified_ns,file_device,file_inode,available,last_seen_scan,show_title,season_number,episode_number)
                            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?,?) ON CONFLICT(path) DO UPDATE SET
                            library_id=excluded.library_id,relative_path=excluded.relative_path,file_name=excluded.file_name,
                            title=CASE WHEN ?=1 THEN excluded.title WHEN media.probe_status='done' AND media.size_bytes=excluded.size_bytes
                                AND media.modified_ns=excluded.modified_ns AND media.title NOT GLOB '[0-9][0-9][0-9]-S*' THEN media.title ELSE excluded.title END,
                            sort_title=CASE WHEN ?=1 THEN excluded.sort_title WHEN media.probe_status='done' AND media.size_bytes=excluded.size_bytes
                                AND media.modified_ns=excluded.modified_ns AND media.title NOT GLOB '[0-9][0-9][0-9]-S*' THEN media.sort_title ELSE excluded.sort_title END,
                            media_type=excluded.media_type,extension=excluded.extension,size_bytes=excluded.size_bytes,modified_ns=excluded.modified_ns,
                            file_device=excluded.file_device,file_inode=excluded.file_inode,
                            available=1,last_seen_scan=excluded.last_seen_scan,
                            probe_status=CASE WHEN media.size_bytes!=excluded.size_bytes OR media.modified_ns!=excluded.modified_ns THEN 'pending' ELSE media.probe_status END,
                            probe_error=CASE WHEN media.size_bytes!=excluded.size_bytes OR media.modified_ns!=excluded.modified_ns THEN NULL ELSE media.probe_error END,
                            show_title=CASE WHEN media.show_title IS NULL OR media.show_title NOT GLOB '*[^0-9]*' THEN excluded.show_title ELSE media.show_title END,
                            season_number=COALESCE(media.season_number,excluded.season_number),episode_number=COALESCE(media.episode_number,excluded.episode_number),
                            updated_at=CURRENT_TIMESTAMP)SQL", values);
                        if (was_renamed) {
                            raw_execute(db, "UPDATE playlist_items SET source_path=?,source_title=? WHERE media_id=?",
                                        {entry.path.string(), entry.title, number_or<std::int64_t>(existing, "id")});
                        }
                        if (existing.is_null()) ++added;
                        else if (is_changed) {
                            ++changed;
                            raw_execute(db, "DELETE FROM loudness_analyses WHERE media_id=?", {number_or<std::int64_t>(existing, "id")});
                        }
                    }
                    unavailable = number_or<std::int64_t>(raw_one(db, "SELECT COUNT(*) AS count FROM media WHERE library_id=? AND available=1 AND last_seen_scan!=?", {library_id, token}), "count");
                    raw_execute(db, "UPDATE media SET available=0,updated_at=CURRENT_TIMESTAMP WHERE library_id=? AND last_seen_scan!=?", {library_id, token});
                    raw_execute(db, "UPDATE libraries SET last_scan_at=CURRENT_TIMESTAMP,last_scan_error=NULL WHERE id=?", {library_id});
                });
            } catch (const std::exception& error_message) {
                scan_error = error_message.what();
                database_.execute("UPDATE libraries SET last_scan_error=? WHERE root_path=?", {*scan_error, library.path.string()});
            }
        }
        auto elapsed = std::chrono::duration<double>(std::chrono::steady_clock::now() - started).count();
        results.push_back({{"library", library.name}, {"discovered", discovered}, {"added", added}, {"changed", changed}, {"renamed", renamed},
                           {"unavailable", unavailable}, {"skipped", skipped}, {"elapsed_seconds", elapsed},
                           {"error", scan_error ? json(*scan_error) : json(nullptr)}});
    }
    return results;
}

json FluxaApp::probe_media(int media_id) {
    auto row = media_row(media_id);
    if (row.is_null()) throw ApiError(404, "Media item is unavailable");
    auto source = resolve_media_path(media_id);
    auto result = run_capture({"ffprobe", "-v", "error", "-show_format", "-show_streams", "-show_chapters", "-of", "json", source.string()});
    if (result.status != 0) {
        auto detail = trim(result.output);
        if (detail.size() > 500) detail.erase(0, detail.size() - 500);
        database_.execute("UPDATE media SET probe_status='error',probe_error=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                          {detail.empty() ? "ffprobe failed" : detail, media_id});
        throw ApiError(422, "ffprobe failed for " + source.filename().string());
    }
    json payload;
    try { payload = json::parse(result.output); } catch (...) { throw ApiError(422, "ffprobe returned invalid JSON"); }
    json videos = json::array(), audios = json::array(), subtitles = json::array();
    for (const auto& stream : payload.value("streams", json::array())) {
        auto type = stream.value("codec_type", "");
        if (type == "video") videos.push_back(stream);
        else if (type == "audio") audios.push_back(stream);
        else if (type == "subtitle") subtitles.push_back(stream);
    }
    auto video = videos.empty() ? json::object() : videos.front();
    auto audio = audios.empty() ? json::object() : audios.front();
    auto format = payload.value("format", json::object());
    auto tags = format.value("tags", json::object());
    auto find_tag = [&](std::initializer_list<const char*> names) -> std::optional<std::string> {
        for (const auto& [key, value] : tags.items()) {
            auto folded = lower(key);
            for (auto name : names) if (folded == name && value.is_string() && !trim(value.get<std::string>()).empty()) return trim(value.get<std::string>());
        }
        return std::nullopt;
    };
    auto parse_int = [](const json& value) -> json {
        if (value.is_number_integer()) return value;
        if (value.is_string()) { try { return std::stoll(value.get<std::string>()); } catch (...) {} }
        return nullptr;
    };
    auto parse_seconds = [](const json& value) -> std::optional<double> {
        try {
            if (value.is_number()) return value.get<double>();
            if (value.is_string()) return std::stod(value.get<std::string>());
        } catch (...) {}
        return std::nullopt;
    };
    std::optional<double> duration;
    if (format.contains("duration")) duration = parse_seconds(format["duration"]);
    if (!duration) {
        for (const auto& stream : payload.value("streams", json::array())) {
            if (stream.contains("duration")) {
                auto candidate = parse_seconds(stream["duration"]);
                if (candidate && (!duration || *candidate > *duration)) duration = candidate;
            }
        }
    }
    json chapter_items = json::array();
    int chapter_number = 1;
    for (const auto& chapter : payload.value("chapters", json::array())) {
        std::optional<double> start, end;
        if (chapter.contains("start_time")) start = parse_seconds(chapter["start_time"]);
        if (chapter.contains("end_time")) end = parse_seconds(chapter["end_time"]);
        if (!start || !end || *end <= *start) continue;
        std::string title = "Chapter " + std::to_string(chapter_number++);
        if (chapter.contains("tags") && chapter["tags"].is_object()) {
            for (const auto& [key, value] : chapter["tags"].items()) if (lower(key) == "title" && value.is_string() && !trim(value.get<std::string>()).empty()) title = trim(value.get<std::string>());
        }
        chapter_items.push_back({{"start_ms", std::max<std::int64_t>(0, std::llround(*start * 1000))},
                                 {"end_ms", std::max<std::int64_t>(0, std::llround(*end * 1000))}, {"title", title}});
    }
    static const std::unordered_set<std::string> text_codecs = {"ass","eia_608","eia_708","mov_text","ssa","subrip","text","webvtt"};
    static const std::unordered_set<std::string> bitmap_codecs = {"dvd_subtitle","dvb_subtitle","hdmv_pgs_subtitle","xsub"};
    json subtitle_items = json::array();
    for (std::size_t ordinal = 0; ordinal < subtitles.size(); ++ordinal) {
        const auto& stream = subtitles[ordinal];
        auto stream_tags = stream.value("tags", json::object());
        std::string language, supplied_title;
        for (const auto& [key, value] : stream_tags.items()) {
            if (!value.is_string()) continue;
            if (lower(key) == "language") language = trim(value.get<std::string>());
            if (lower(key) == "title") supplied_title = trim(value.get<std::string>());
        }
        auto disposition = stream.value("disposition", json::object());
        bool hearing = disposition.value("hearing_impaired", 0) != 0;
        std::string title = supplied_title.empty() ? (language.empty() ? "Unknown" : language) + " captions" : supplied_title;
        if (hearing && lower(title).find("sdh") == std::string::npos) title += " (SDH)";
        auto codec = stream.value("codec_name", "unknown");
        auto kind = text_codecs.contains(codec) ? "text" : (bitmap_codecs.contains(codec) ? "bitmap" : "unsupported");
        subtitle_items.push_back({{"ordinal", ordinal}, {"stream_index", stream.value("index", static_cast<int>(ordinal))},
                                  {"codec", codec}, {"language", language.empty() ? json(nullptr) : json(language)}, {"title", title},
                                  {"kind", kind}, {"default", disposition.value("default", 0) != 0},
                                  {"forced", disposition.value("forced", 0) != 0}, {"hearing_impaired", hearing}});
    }
    auto file_episode = episode_info(source.stem().string());
    std::string effective_title = file_episode.title.value_or(find_tag({"title"}).value_or(string_or(row, "title")));
    auto show = find_tag({"show", "album"});
    auto season_tag = find_tag({"season_number"});
    auto episode_tag = find_tag({"episode_id", "episode_sort"});
    auto container = format.value("format_name", "");
    if (auto comma = container.find(','); comma != std::string::npos) container.erase(comma);
    database_.execute(R"SQL(UPDATE media SET title=?,sort_title=?,probe_status='done',probe_error=NULL,duration_ms=?,container=?,
        video_codec=?,width=?,height=?,audio_codec=?,audio_channels=?,audio_tracks=?,subtitle_tracks=?,chapters_json=?,subtitle_streams_json=?,
        show_title=COALESCE(?,show_title),season_number=COALESCE(?,season_number),episode_number=COALESCE(?,episode_number),updated_at=CURRENT_TIMESTAMP
        WHERE id=?)SQL", {
        effective_title, sort_key(effective_title), duration ? json(std::llround(*duration * 1000)) : json(nullptr),
        container.empty() ? json(nullptr) : json(container), video.value("codec_name", json(nullptr)), video.value("width", json(nullptr)),
        video.value("height", json(nullptr)), audio.value("codec_name", json(nullptr)), audio.value("channels", json(nullptr)),
        audios.size(), subtitles.size(), chapter_items.dump(), subtitle_items.dump(), show ? json(*show) : json(nullptr),
        season_tag ? parse_int(*season_tag) : json(nullptr), episode_tag ? parse_int(*episode_tag) : json(nullptr), media_id
    });
    return media_detail(media_id, false);
}

void FluxaApp::start_probe_worker() {
    if (probe_running_.exchange(true)) return;
    if (probe_thread_.joinable()) probe_thread_.join();
    probe_thread_ = std::jthread([this](std::stop_token stop) {
        struct Reset { std::atomic<bool>& flag; ~Reset() { flag = false; } } reset{probe_running_};
        while (!stop.stop_requested() && !stopping_) {
            if (compatibility_.active_count()) { std::this_thread::sleep_for(1s); continue; }
            auto row = database_.one(R"SQL(SELECT id FROM media WHERE available=1 AND media_type='video' AND probe_status='pending'
                ORDER BY size_bytes,id LIMIT 1)SQL");
            if (row.is_null()) break;
            int id = number_or<int>(row, "id");
            database_.execute("UPDATE media SET probe_status='probing' WHERE id=?", {id});
            try { probe_media(id); } catch (...) {}
            ++probe_completed_;
        }
    });
}

fs::path FluxaApp::thumbnail_path(const json& row) const {
    return thumbnail_dir_ / (std::to_string(number_or<std::int64_t>(row, "id")) + "-" +
                             std::to_string(number_or<std::int64_t>(row, "modified_ns")) + "-v2.jpg");
}

std::optional<std::string> FluxaApp::fredplayer_relative_path(const json& row) const {
    if (!config_.fredplayer_artwork.enabled || string_or(row, "media_type") != "audio") return std::nullopt;
    fs::path source = string_or(row, "path");
    const auto& root = config_.fredplayer_artwork.music_dir;
    auto relative = source.lexically_relative(root);
    if (relative.empty() || relative.is_absolute()) return std::nullopt;
    for (const auto& component : relative) if (component == "..") return std::nullopt;
    return relative.generic_string();
}

std::optional<ArtworkResponse> FluxaApp::fredplayer_artwork(int media_id) {
    auto row = media_row(media_id);
    if (row.is_null()) throw ApiError(404, "Media item not found");
    auto relative = fredplayer_relative_path(row);
    if (!relative) return std::nullopt;

    httplib::Client client(config_.fredplayer_artwork.host, config_.fredplayer_artwork.port);
    client.set_connection_timeout(2, 0);
    client.set_read_timeout(5, 0);
    httplib::Headers headers{{"Authorization", "Bearer " + config_.fredplayer_artwork.auth_token}};
    auto response = client.Get("/api/artwork/" + url_encode(*relative, true), headers);
    if (!response || response->status != 200 || response->body.empty() || response->body.size() > 8 * 1024 * 1024) {
        return std::nullopt;
    }
    auto content_type = response->get_header_value("Content-Type");
    if (!content_type.starts_with("image/")) return std::nullopt;
    return ArtworkResponse{std::move(response->body), std::move(content_type)};
}

std::optional<ArtworkResponse> FluxaApp::fredplayer_artwork_path(const std::string& relative_path) {
    if (!config_.fredplayer_artwork.enabled || relative_path.empty() || relative_path.size() > 4096) return std::nullopt;
    fs::path candidate = relative_path;
    if (candidate.is_absolute()) return std::nullopt;
    for (const auto& component : candidate) if (component == "..") return std::nullopt;
    httplib::Client client(config_.fredplayer_artwork.host, config_.fredplayer_artwork.port);
    client.set_connection_timeout(2, 0);
    client.set_read_timeout(5, 0);
    httplib::Headers headers{{"Authorization", "Bearer " + config_.fredplayer_artwork.auth_token}};
    auto response = client.Get("/api/artwork/" + url_encode(candidate.generic_string(), true), headers);
    if (!response || response->status != 200 || response->body.empty() || response->body.size() > 8 * 1024 * 1024) return std::nullopt;
    auto content_type = response->get_header_value("Content-Type");
    if (!content_type.starts_with("image/")) return std::nullopt;
    return ArtworkResponse{std::move(response->body), std::move(content_type)};
}

json FluxaApp::fredplayer_library(const httplib::Request& request) {
    if (!config_.fredplayer_artwork.enabled) throw ApiError(503, "FredPlayer is not configured");
    httplib::Client client(config_.fredplayer_artwork.host, config_.fredplayer_artwork.port);
    client.set_connection_timeout(2, 0);
    client.set_read_timeout(30, 0);
    httplib::Headers headers{{"Authorization", "Bearer " + config_.fredplayer_artwork.auth_token}};
    auto response = client.Get("/api/library", headers);
    if (!response || response->status != 200) throw ApiError(502, "FredPlayer library is unavailable");
    auto tracks = json::parse(response->body);
    if (!tracks.is_array()) throw ApiError(502, "FredPlayer returned an invalid library");

    std::unordered_set<std::string> playlist_paths;
    std::vector<std::string> playlist_order;
    auto playlist_name = request.has_param("playlist") ? request.get_param_value("playlist") : "";
    if (!playlist_name.empty()) {
        auto playlist_response = client.Get("/api/playlists/" + url_encode(playlist_name), headers);
        if (!playlist_response || playlist_response->status != 200) throw ApiError(404, "FredPlayer playlist was not found");
        auto playlist = json::parse(playlist_response->body);
        for (const auto& path : playlist.value("tracks", json::array())) {
            if (path.is_string()) {
                playlist_order.push_back(path.get<std::string>());
                playlist_paths.insert(playlist_order.back());
            }
        }
    }

    auto query = lower(trim(request.has_param("q") ? request.get_param_value("q") : ""));
    auto selected_album = request.has_param("album") ? request.get_param_value("album") : "";
    auto selected_artist = request.has_param("artist") ? request.get_param_value("artist") : "";
    auto limit_value = query_integer(request, "limit").value_or(120);
    auto offset_value = query_integer(request, "offset").value_or(0);
    auto limit = static_cast<std::size_t>(std::clamp<std::int64_t>(limit_value, 1, 250));
    auto offset = static_cast<std::size_t>(std::max<std::int64_t>(offset_value, 0));
    json matched = json::array();
    for (const auto& track : tracks) {
        if (!track.is_object()) continue;
        auto path = track.value("path", "");
        auto title = track.value("title", fs::path(path).stem().string());
        auto artist = track.value("artist", "");
        auto album = track.value("album", "");
        if (!playlist_name.empty() && !playlist_paths.contains(path)) continue;
        if (!selected_album.empty() && album != selected_album) continue;
        if (!selected_artist.empty() && artist != selected_artist) continue;
        auto haystack = lower(title + "\n" + artist + "\n" + album + "\n" + path);
        if (!query.empty() && haystack.find(query) == std::string::npos) continue;
        matched.push_back({
            {"id", nullptr}, {"available", true}, {"library_name", "FredPlayer"},
            {"title", title}, {"artist", artist}, {"album", album},
            {"media_type", "audio"}, {"extension", fs::path(path).extension().string()},
            {"duration_ms", 0}, {"size_bytes", 0}, {"show_title", album.empty() ? json(nullptr) : json(album)},
            {"fredplayer_path", path},
            {"thumbnail_url", "/api/fredplayer/artwork?path=" + url_encode(path)},
            {"progress", {{"position_ms", 0}, {"duration_ms", 0}, {"completed", false}}},
            {"analysis", {{"status", "fredplayer"}, {"spike_count", 0}}}
        });
    }
    if (!playlist_order.empty()) {
        std::unordered_map<std::string, json> by_path;
        for (const auto& item : matched) by_path.emplace(item.value("fredplayer_path", ""), item);
        json ordered = json::array();
        for (const auto& path : playlist_order) {
            if (auto item = by_path.find(path); item != by_path.end()) ordered.push_back(item->second);
        }
        matched = std::move(ordered);
    }
    auto total = matched.size();
    json items = json::array();
    for (std::size_t index = offset; index < total && items.size() < limit; ++index) items.push_back(matched[index]);
    return {{"items", std::move(items)}, {"total", total}, {"limit", limit}, {"offset", offset}};
}

json FluxaApp::fredplayer_collections(const httplib::Request& request) {
    if (!config_.fredplayer_artwork.enabled) throw ApiError(503, "FredPlayer is not configured");
    httplib::Client client(config_.fredplayer_artwork.host, config_.fredplayer_artwork.port);
    client.set_connection_timeout(2, 0);
    client.set_read_timeout(30, 0);
    httplib::Headers headers{{"Authorization", "Bearer " + config_.fredplayer_artwork.auth_token}};
    auto kind = request.has_param("kind") ? request.get_param_value("kind") : "";
    if (kind == "playlists") {
        auto response = client.Get("/api/playlists", headers);
        if (!response || response->status != 200) throw ApiError(502, "FredPlayer playlists are unavailable");
        auto playlists = json::parse(response->body);
        json items = json::array();
        for (const auto& playlist : playlists) {
            if (!playlist.is_object()) continue;
            items.push_back({
                {"collection_type", "playlist"}, {"name", playlist.value("name", "")},
                {"title", playlist.value("name", "")}, {"count", playlist.value("count", 0)},
                {"thumbnail_url", nullptr}
            });
        }
        return {{"items", items}, {"total", items.size()}};
    }
    if (kind != "albums") throw ApiError(400, "Unknown FredPlayer collection type");
    auto response = client.Get("/api/library", headers);
    if (!response || response->status != 200) throw ApiError(502, "FredPlayer library is unavailable");
    auto tracks = json::parse(response->body);
    struct Album { std::string artist; std::string title; std::string artwork_path; int count{}; };
    std::map<std::string, Album> albums;
    for (const auto& track : tracks) {
        if (!track.is_object()) continue;
        auto album = trim(track.value("album", ""));
        auto artist = trim(track.value("artist", ""));
        auto path = track.value("path", "");
        if (album.empty()) album = "Unknown album";
        if (artist.empty()) artist = "Unknown artist";
        auto key = lower(artist) + "\n" + lower(album);
        auto& entry = albums[key];
        if (entry.count == 0) entry = {artist, album, path, 0};
        ++entry.count;
    }
    json items = json::array();
    for (const auto& [key, album] : albums) {
        (void)key;
        items.push_back({
            {"collection_type", "album"}, {"name", album.title}, {"title", album.title},
            {"artist", album.artist}, {"count", album.count},
            {"thumbnail_url", album.artwork_path.empty() ? json(nullptr)
                : json("/api/fredplayer/artwork?path=" + url_encode(album.artwork_path))}
        });
    }
    auto query = lower(trim(request.has_param("q") ? request.get_param_value("q") : ""));
    json matched = json::array();
    for (const auto& item : items) {
        if (query.empty() || lower(item.value("title", "") + "\n" + item.value("artist", "")).find(query) != std::string::npos) {
            matched.push_back(item);
        }
    }
    auto total = matched.size();
    auto limit = static_cast<std::size_t>(std::clamp<std::int64_t>(query_integer(request, "limit").value_or(120), 1, 250));
    auto offset = static_cast<std::size_t>(std::max<std::int64_t>(query_integer(request, "offset").value_or(0), 0));
    json page = json::array();
    for (std::size_t index = offset; index < total && page.size() < limit; ++index) page.push_back(matched[index]);
    return {{"items", page}, {"total", total}, {"limit", limit}, {"offset", offset}};
}

bool FluxaApp::redeem_fredplayer_grant(const std::string& grant) {
    if (!config_.fredplayer_artwork.enabled || grant.size() < 20 || grant.size() > 128 ||
        !std::all_of(grant.begin(), grant.end(), [](unsigned char value) {
            return std::isalnum(value) || value == '-' || value == '_';
        })) return false;
    httplib::Client client(config_.fredplayer_artwork.host, config_.fredplayer_artwork.port);
    client.set_connection_timeout(2, 0);
    client.set_read_timeout(5, 0);
    httplib::Headers headers{{"Authorization", "Bearer " + config_.fredplayer_artwork.auth_token}};
    auto response = client.Post("/api/fluxa-grant/" + grant, headers, "", "application/json");
    return response && response->status == 200;
}

std::string FluxaApp::fredplayer_launch_ticket(const json& request) {
    auto track_path = request.value("track_path", "");
    auto playlist_name = request.value("playlist_name", "");
    auto return_url = request.value("return_url", "");
    auto track_paths = request.value("track_paths", json::array());
    if (!config_.fredplayer_artwork.enabled || return_url.empty() || return_url.size() > 4096 ||
        playlist_name.size() > 500 ||
        (!track_paths.is_array() || track_paths.size() > 10000) ||
        (track_path.empty() && track_paths.empty() && playlist_name.empty())) {
        throw ApiError(400, "Invalid FredPlayer launch request");
    }
    for (const auto& path : track_paths) {
        if (!path.is_string() || path.get_ref<const std::string&>().empty() ||
            path.get_ref<const std::string&>().size() > 4096) {
            throw ApiError(400, "Invalid FredPlayer queue");
        }
    }
    httplib::Client client(config_.fredplayer_artwork.host, config_.fredplayer_artwork.port);
    client.set_connection_timeout(2, 0);
    client.set_read_timeout(5, 0);
    httplib::Headers headers{{"Authorization", "Bearer " + config_.fredplayer_artwork.auth_token}};
    json launch{{"trackPath", track_path}, {"trackPaths", track_paths}, {"playlistName", playlist_name},
        {"sourceName", request.value("source_name", "")},
        {"sourceKind", request.value("source_kind", "Queue")},
        {"shuffle", request.value("shuffle", false)},
        {"startPath", request.value("start_path", "")}, {"returnUrl", return_url}};
    auto response = client.Post("/api/fluxa-launch-ticket", headers,
        launch.dump(), "application/json");
    if (!response || response->status != 200) throw ApiError(502, "FredPlayer launch could not be authorized");
    auto payload = json::parse(response->body);
    auto ticket = payload.value("ticket", "");
    if (ticket.size() < 20 || ticket.size() > 128) throw ApiError(502, "FredPlayer returned an invalid launch ticket");
    return ticket;
}

fs::path FluxaApp::chapter_thumbnail_path(const json& row, int index) const {
    return chapter_thumbnail_dir_ / (std::to_string(number_or<std::int64_t>(row, "id")) + "-" +
                                     std::to_string(number_or<std::int64_t>(row, "modified_ns")) + "-" +
                                     std::to_string(index) + "-v1.jpg");
}

bool FluxaApp::generate_thumbnail(int media_id) {
    auto row = media_row(media_id);
    if (row.is_null() || string_or(row, "media_type") != "video") return false;
    auto target = thumbnail_path(row);
    if (fs::is_regular_file(target)) return true;
    try {
        auto duration = number_or<std::int64_t>(row, "duration_ms") / 1000.0;
        double seek = duration > 0 ? std::min(300.0, std::max(15.0, duration * 0.12)) : 30.0;
        auto temporary = fs::path(target.string() + "." + std::to_string(::getpid()) + ".tmp");
        std::ostringstream position; position << std::fixed << std::setprecision(3) << seek;
        auto result = run_capture({"nice", "-n", "10", "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-threads", "1", "-ss", position.str(), "-i", resolve_media_path(media_id).string(), "-map", "0:v:0", "-frames:v", "1",
            "-vf", "scale=w='trunc(iw*sar/2)*2':h=ih,setsar=1,scale=480:-2:force_original_aspect_ratio=decrease",
            "-q:v", "4", "-c:v", "mjpeg", "-an", "-sn", "-dn", "-threads", "1", "-f", "image2", temporary.string()});
        if (result.status == 0 && fs::is_regular_file(temporary) && fs::file_size(temporary) > 0) { fs::rename(temporary, target); return true; }
        std::error_code error; fs::remove(temporary, error);
    } catch (...) {}
    return false;
}

fs::path FluxaApp::thumbnail(int media_id) {
    auto row = media_row(media_id);
    if (row.is_null() || string_or(row, "media_type") != "video") throw ApiError(404, "Video item not found");
    auto target = thumbnail_path(row);
    if (fs::is_regular_file(target)) return target;
    {
        std::lock_guard lock(artwork_mutex_);
        if (thumbnail_generating_.contains(media_id)) return {};
        thumbnail_generating_.insert(media_id);
    }
    ++artwork_pending_; ++episode_pending_;
    std::thread([this, media_id] {
        generate_thumbnail(media_id);
        std::lock_guard lock(artwork_mutex_);
        thumbnail_generating_.erase(media_id);
        --artwork_pending_; --episode_pending_;
    }).detach();
    return {};
}

bool FluxaApp::generate_chapter_thumbnail(int media_id, int index) {
    auto row = media_row(media_id);
    auto chapters = parse_json_field(row, "chapters_json");
    if (row.is_null() || index < 0 || index >= static_cast<int>(chapters.size())) return false;
    auto target = chapter_thumbnail_path(row, index);
    if (fs::is_regular_file(target)) return true;
    try {
        auto chapter = chapters.at(index);
        auto start = chapter.value("start_ms", 0LL), end = chapter.value("end_ms", start);
        auto seek = std::max<std::int64_t>(0, start + std::min<std::int64_t>(3000, std::max<std::int64_t>(500, (end - start) / 8)));
        auto temporary = fs::path(target.string() + "." + std::to_string(::getpid()) + ".tmp");
        std::ostringstream position; position << std::fixed << std::setprecision(3) << seek / 1000.0;
        auto result = run_capture({"nice", "-n", "10", "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-threads", "1", "-ss", position.str(), "-i", resolve_media_path(media_id).string(), "-map", "0:v:0", "-frames:v", "1",
            "-vf", "scale=w='trunc(iw*sar/2)*2':h=ih,setsar=1,scale=320:-2:force_original_aspect_ratio=decrease",
            "-q:v", "5", "-c:v", "mjpeg", "-an", "-sn", "-dn", "-threads", "1", "-f", "image2", temporary.string()});
        if (result.status == 0 && fs::is_regular_file(temporary) && fs::file_size(temporary) > 0) { fs::rename(temporary, target); return true; }
        std::error_code error; fs::remove(temporary, error);
    } catch (...) {}
    return false;
}

fs::path FluxaApp::chapter_thumbnail(int media_id, int index) {
    auto row = media_row(media_id);
    auto chapters = parse_json_field(row, "chapters_json");
    if (row.is_null() || string_or(row, "media_type") != "video") throw ApiError(404, "Video item not found");
    if (index < 0 || index >= static_cast<int>(chapters.size())) throw ApiError(404, "Chapter not found");
    auto target = chapter_thumbnail_path(row, index);
    if (fs::is_regular_file(target)) return target;
    auto key = std::make_pair(media_id, index);
    {
        std::lock_guard lock(artwork_mutex_);
        if (chapter_generating_.contains(key)) return {};
        chapter_generating_.insert(key);
    }
    ++artwork_pending_; ++chapter_pending_;
    std::thread([this, media_id, index, key] {
        generate_chapter_thumbnail(media_id, index);
        std::lock_guard lock(artwork_mutex_);
        chapter_generating_.erase(key);
        --artwork_pending_; --chapter_pending_;
    }).detach();
    return {};
}

void FluxaApp::start_artwork_backfill() {
    if (artwork_running_.exchange(true)) return;
    if (artwork_thread_.joinable()) artwork_thread_.join();
    artwork_thread_ = std::jthread([this](std::stop_token stop) {
        struct Reset { std::atomic<bool>& flag; ~Reset() { flag = false; } } reset{artwork_running_};
        while (!stop.stop_requested() && !stopping_) {
            auto rows = database_.query("SELECT id,modified_ns,chapters_json FROM media WHERE available=1 AND media_type='video' ORDER BY id");
            for (const auto& row : rows) {
                if (stop.stop_requested() || stopping_) return;
                while (compatibility_.active_count() && !stop.stop_requested()) std::this_thread::sleep_for(1s);
                int id = number_or<int>(row, "id");
                if (!fs::is_regular_file(thumbnail_path(row))) {
                    ++artwork_pending_; ++episode_pending_;
                    generate_thumbnail(id);
                    --artwork_pending_; --episode_pending_;
                }
                auto chapters = parse_json_field(row, "chapters_json");
                for (int index = 0; index < static_cast<int>(chapters.size()); ++index) {
                    if (stop.stop_requested()) return;
                    if (!fs::is_regular_file(chapter_thumbnail_path(row, index))) {
                        ++artwork_pending_; ++chapter_pending_;
                        generate_chapter_thumbnail(id, index);
                        --artwork_pending_; --chapter_pending_;
                    }
                }
                ++artwork_completed_;
            }
            for (int i = 0; i < 3000 && !stop.stop_requested(); ++i) std::this_thread::sleep_for(100ms);
        }
    });
}

fs::path FluxaApp::caption(int media_id, int ordinal) {
    auto row = media_row(media_id);
    if (row.is_null() || string_or(row, "media_type") != "video") throw ApiError(404, "Video item not found");
    bool found = false;
    for (const auto& track : parse_json_field(row, "subtitle_streams_json")) {
        if (track.value("ordinal", -1) == ordinal) {
            found = true;
            if (track.value("kind", "") != "text") throw ApiError(422, "This image-based caption track is provided through the compatibility stream");
            break;
        }
    }
    if (!found) throw ApiError(404, "Caption track not found");
    auto target = caption_dir_ / (std::to_string(media_id) + "-" + std::to_string(number_or<std::int64_t>(row, "modified_ns")) +
                                  "-" + std::to_string(ordinal) + "-v1.vtt");
    if (fs::is_regular_file(target)) return target;
    auto temporary = fs::path(target.string() + "." + std::to_string(::getpid()) + ".tmp");
    auto result = run_capture({"ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i",
        resolve_media_path(media_id).string(), "-map", "0:s:" + std::to_string(ordinal), "-c:s", "webvtt", "-f", "webvtt", temporary.string()});
    if (result.status != 0 || !fs::is_regular_file(temporary)) {
        std::error_code error; fs::remove(temporary, error);
        auto detail = trim(result.output); if (detail.size() > 500) detail.erase(0, detail.size() - 500);
        throw ApiError(422, detail.empty() ? "Caption conversion failed" : detail);
    }
    fs::rename(temporary, target);
    return target;
}

struct LoudnessSample {
    std::int64_t time_ms{};
    double momentary{};
    double short_term{};
};

double median(std::vector<double> values) {
    if (values.empty()) return -70;
    std::sort(values.begin(), values.end());
    auto middle = values.size() / 2;
    return values.size() % 2 ? values[middle] : (values[middle - 1] + values[middle]) / 2;
}

double percentile(std::vector<double> values, double fraction) {
    if (values.empty()) return -70;
    std::sort(values.begin(), values.end());
    auto index = static_cast<std::size_t>(std::llround((values.size() - 1) * fraction));
    return values[std::min(index, values.size() - 1)];
}

void FluxaApp::analyze_media(int media_id) {
    auto row = media_row(media_id);
    if (row.is_null()) throw ApiError(404, "Media item is unavailable");
    database_.execute(R"SQL(INSERT INTO loudness_analyses(media_id,analyzer_version,status,error) VALUES(?,1,'running',NULL)
        ON CONFLICT(media_id) DO UPDATE SET analyzer_version=1,status='running',error=NULL,updated_at=CURRENT_TIMESTAMP)SQL", {media_id});
    try {
        auto result = run_capture({"ffmpeg", "-nostdin", "-hide_banner", "-nostats", "-loglevel", "verbose", "-i",
            resolve_media_path(media_id).string(), "-map", "0:a:0", "-filter:a", "ebur128=peak=true:framelog=verbose", "-f", "null", "-"});
        if (result.status != 0) throw std::runtime_error("FFmpeg could not analyze " + string_or(row, "file_name"));
        static const std::regex sample_pattern(R"(\bt:\s*(\d+(?:\.\d+)?)\b.*?\bM:\s*(-?(?:inf|\d+(?:\.\d+)?))\s+S:\s*(-?(?:inf|\d+(?:\.\d+)?)))", std::regex::icase);
        static const std::regex integrated_pattern(R"(^\s*I:\s*(-?\d+(?:\.\d+)?)\s+LUFS\s*$)", std::regex::icase);
        static const std::regex peak_pattern(R"(^\s*Peak:\s*(-?\d+(?:\.\d+)?)\s+dBFS\s*$)", std::regex::icase);
        std::map<std::int64_t, LoudnessSample> buckets;
        std::optional<double> integrated, true_peak;
        std::stringstream lines(result.output);
        std::string line;
        while (std::getline(lines, line)) {
            std::smatch match;
            if (std::regex_search(line, match, sample_pattern)) {
                try {
                    double momentary = std::stod(match[2]), short_term = std::stod(match[3]);
                    if (std::isfinite(momentary) && std::isfinite(short_term) && short_term > -70) {
                        auto time_ms = std::llround(std::stod(match[1]) * 1000);
                        auto bucket = time_ms / 1000;
                        auto found = buckets.find(bucket);
                        if (found == buckets.end() || short_term > found->second.short_term) buckets[bucket] = {time_ms, momentary, short_term};
                    }
                } catch (...) {}
            } else if (std::regex_match(line, match, integrated_pattern)) {
                integrated = std::stod(match[1]);
            } else if (std::regex_match(line, match, peak_pattern)) {
                true_peak = std::stod(match[1]);
            }
        }
        std::vector<LoudnessSample> samples;
        for (const auto& [bucket, sample] : buckets) { (void)bucket; samples.push_back(sample); }
        if (samples.empty()) throw std::runtime_error("No usable loudness samples were found");
        json segments = json::array();
        std::size_t index = 0;
        while (index < samples.size()) {
            auto current_time = samples[index].time_ms;
            std::vector<double> prior;
            for (const auto& sample : samples) {
                if (sample.time_ms >= current_time - 45000 && sample.time_ms < current_time - 2000) prior.push_back(sample.short_term);
            }
            if (prior.size() < 8) { ++index; continue; }
            std::vector<double> future;
            for (std::size_t i = index; i < std::min(samples.size(), index + 8); ++i) future.push_back(samples[i].short_term);
            if (future.size() < 4) break;
            double baseline = median(prior), loud_level = median(future);
            if (loud_level - baseline < 5) { ++index; continue; }
            double threshold = baseline + 2.75;
            std::size_t end_index = index;
            int quiet_run = 0;
            while (end_index + 1 < samples.size()) {
                ++end_index;
                if (samples[end_index].short_term < threshold) {
                    if (++quiet_run >= 5) { end_index -= quiet_run; break; }
                } else quiet_run = 0;
            }
            end_index = std::max(index, end_index);
            std::vector<double> region;
            for (std::size_t i = index; i <= end_index; ++i) region.push_back(samples[i].short_term);
            loud_level = percentile(region, 0.85);
            double reduction = std::min(12.0, std::max({0.0, loud_level - baseline - 3.0, loud_level + 20.0}));
            reduction = std::round(reduction * 100) / 100;
            if (reduction >= 1) {
                auto loud_start = std::max<std::int64_t>(0, samples[index].time_ms - 1500);
                auto loud_end = samples[end_index].time_ms;
                segments.push_back({
                    {"attack_start_ms", std::max<std::int64_t>(0, loud_start - 8000)}, {"loud_start_ms", loud_start},
                    {"loud_end_ms", loud_end}, {"release_end_ms", loud_end + 6000}, {"reduction_db", reduction},
                    {"baseline_lufs", std::round(baseline * 100) / 100}, {"loud_lufs", std::round(loud_level * 100) / 100}
                });
            }
            index = std::max(index + 1, end_index + 1);
        }
        auto last_time = samples.back().time_ms;
        for (const auto& segment : segments) last_time = std::max<std::int64_t>(last_time, segment.value("release_end_ms", std::int64_t{0}));
        json envelope = json::array();
        double previous = std::numeric_limits<double>::quiet_NaN();
        for (std::int64_t stamp = 0; stamp <= last_time + 1000; stamp += 1000) {
            double gain = 0;
            for (const auto& segment : segments) {
                auto attack_start = segment.value("attack_start_ms", 0LL), loud_start = segment.value("loud_start_ms", 0LL);
                auto loud_end = segment.value("loud_end_ms", 0LL), release_end = segment.value("release_end_ms", 0LL);
                double reduction = segment.value("reduction_db", 0.0), candidate = 0;
                if (stamp < attack_start || stamp > release_end) continue;
                if (stamp < loud_start) candidate = -reduction * (stamp - attack_start) / std::max<std::int64_t>(1, loud_start - attack_start);
                else if (stamp <= loud_end) candidate = -reduction;
                else candidate = -reduction * (1.0 - (stamp - loud_end) / static_cast<double>(std::max<std::int64_t>(1, release_end - loud_end)));
                gain = std::min(gain, candidate);
            }
            gain = std::round(gain * 100) / 100;
            if (envelope.empty() || gain != previous || gain != 0) envelope.push_back({stamp, gain});
            previous = gain;
        }
        if (envelope.empty() || envelope.back()[0] != last_time || envelope.back()[1] != 0) envelope.push_back({last_time, 0});
        database_.execute(R"SQL(UPDATE loudness_analyses SET analyzer_version=1,status='done',integrated_lufs=?,true_peak_db=?,
            sample_interval_ms=1000,envelope_json=?,spike_segments_json=?,error=NULL,analyzed_at=?,updated_at=CURRENT_TIMESTAMP WHERE media_id=?)SQL",
            {integrated ? json(*integrated) : json(nullptr), true_peak ? json(*true_peak) : json(nullptr), envelope.dump(), segments.dump(), iso_timestamp(), media_id});
    } catch (const std::exception& error) {
        database_.execute(R"SQL(INSERT INTO loudness_analyses(media_id,analyzer_version,status,error) VALUES(?,1,'error',?)
            ON CONFLICT(media_id) DO UPDATE SET status='error',error=excluded.error,updated_at=CURRENT_TIMESTAMP)SQL", {media_id, error.what()});
        throw;
    }
}

bool FluxaApp::start_analysis(int media_id) {
    if (media_row(media_id).is_null()) throw ApiError(404, "Media item not found");
    {
        std::lock_guard lock(analysis_mutex_);
        if (analysis_jobs_.contains(media_id)) return false;
        analysis_jobs_.insert(media_id);
    }
    std::thread([this, media_id] {
        try { analyze_media(media_id); } catch (...) {}
        std::lock_guard lock(analysis_mutex_);
        analysis_jobs_.erase(media_id);
    }).detach();
    return true;
}

json FluxaApp::import_plex_playlists(const fs::path& requested_path) {
    std::error_code path_error;
    auto plex_path = fs::canonical(requested_path, path_error);
    if (path_error || !fs::is_regular_file(plex_path)) throw std::runtime_error("Plex library database not found: " + requested_path.string());
    sqlite3* plex{};
    std::string uri = "file:" + plex_path.string() + "?mode=ro";
    if (sqlite3_open_v2(uri.c_str(), &plex, SQLITE_OPEN_READONLY | SQLITE_OPEN_URI, nullptr) != SQLITE_OK) {
        std::string error = plex ? sqlite3_errmsg(plex) : "unknown error";
        if (plex) sqlite3_close(plex);
        throw std::runtime_error("Could not read Plex playlists: " + error);
    }
    struct Close { sqlite3* db; ~Close() { sqlite3_close(db); } } close{plex};
    sqlite3_busy_timeout(plex, 30000);
    raw_execute(plex, "PRAGMA query_only=ON");
    auto playlists = raw_query(plex, R"SQL(SELECT id,title,title_sort,media_item_count,updated_at FROM metadata_items
        WHERE metadata_type=15 AND deleted_at IS NULL ORDER BY title_sort COLLATE NOCASE,id)SQL");
    std::unordered_map<std::string, int> exact, canonical;
    std::unordered_map<int, std::string> media_types;
    for (const auto& row : database_.query("SELECT id,path,media_type FROM media WHERE available=1 ORDER BY id")) {
        int id = number_or<int>(row, "id");
        auto path = string_or(row, "path");
        exact[path] = id;
        std::error_code error;
        auto resolved = fs::canonical(path, error);
        if (!error) canonical.try_emplace(resolved.string(), id);
        media_types[id] = string_or(row, "media_type");
    }
    struct Imported { json playlist; json items; };
    std::vector<Imported> imported;
    std::int64_t matched_total = 0, item_total = 0;
    for (const auto& playlist : playlists) {
        auto source_rows = raw_query(plex, R"SQL(
            SELECT g.id AS generator_id,g.metadata_item_id,g.[order] AS source_order,item.title AS item_title,
                   item.metadata_type,mp.file AS source_path
            FROM play_queue_generators g LEFT JOIN metadata_items item ON item.id=g.metadata_item_id
            LEFT JOIN media_parts mp ON mp.id=(SELECT mp2.id FROM media_items mi2 JOIN media_parts mp2 ON mp2.media_item_id=mi2.id
                WHERE mi2.metadata_item_id=g.metadata_item_id AND mi2.deleted_at IS NULL AND mp2.deleted_at IS NULL
                ORDER BY mi2.id,mp2.[index],mp2.id LIMIT 1)
            WHERE g.playlist_id=? ORDER BY g.[order],g.id)SQL", {number_or<std::int64_t>(playlist, "id")});
        json items = json::array();
        int position = 0;
        for (const auto& source : source_rows) {
            auto source_path = string_optional(source, "source_path");
            std::optional<int> media_id;
            if (source_path) {
                if (auto found = exact.find(*source_path); found != exact.end()) media_id = found->second;
                else {
                    std::error_code error;
                    auto resolved = fs::canonical(*source_path, error);
                    if (!error) if (auto found = canonical.find(resolved.string()); found != canonical.end()) media_id = found->second;
                }
            }
            if (media_id) ++matched_total;
            std::string title = string_or(source, "item_title");
            if (title.empty()) title = source_path ? fs::path(*source_path).stem().string() : "Unavailable Plex item";
            std::string kind;
            if (media_id) kind = media_types[*media_id];
            else if (source_path) {
                auto extension = lower(fs::path(*source_path).extension().string());
                if (kVideoExtensions.contains(extension)) kind = "video";
                else if (kAudioExtensions.contains(extension)) kind = "audio";
            }
            items.push_back({{"position", position++}, {"media_id", media_id ? json(*media_id) : json(nullptr)},
                             {"source_item_id", std::to_string(number_or<std::int64_t>(source, "generator_id"))},
                             {"source_order", source.value("source_order", json(nullptr))},
                             {"source_path", source_path ? json(*source_path) : json(nullptr)}, {"source_title", title}, {"kind", kind}});
            ++item_total;
        }
        imported.push_back({playlist, std::move(items)});
    }
    database_.transaction([&](sqlite3* db) {
        std::vector<std::string> source_ids;
        for (const auto& entry : imported) {
            auto source_id = std::to_string(number_or<std::int64_t>(entry.playlist, "id"));
            source_ids.push_back(source_id);
            std::unordered_set<std::string> kinds;
            int matched = 0;
            for (const auto& item : entry.items) {
                auto kind = item.value("kind", ""); if (!kind.empty()) kinds.insert(kind);
                if (!item["media_id"].is_null()) ++matched;
            }
            auto kind = kinds.size() == 1 ? *kinds.begin() : "mixed";
            auto title = string_or(entry.playlist, "title", "Plex playlist " + source_id);
            auto supplied_sort = string_or(entry.playlist, "title_sort");
            raw_execute(db, R"SQL(INSERT INTO playlists(source,source_id,title,sort_title,kind,source_item_count,matched_item_count,source_updated_at)
                VALUES('plex',?,?,?,?,?,?,?) ON CONFLICT(source,source_id) DO UPDATE SET title=excluded.title,sort_title=excluded.sort_title,
                kind=excluded.kind,source_item_count=excluded.source_item_count,matched_item_count=excluded.matched_item_count,
                source_updated_at=excluded.source_updated_at,imported_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP)SQL",
                {source_id, title, supplied_sort.empty() ? sort_key(title) : supplied_sort, kind, entry.items.size(), matched,
                 entry.playlist.value("updated_at", json(nullptr))});
            auto destination = raw_one(db, "SELECT id FROM playlists WHERE source='plex' AND source_id=?", {source_id});
            int playlist_id = number_or<int>(destination, "id");
            raw_execute(db, "DELETE FROM playlist_items WHERE playlist_id=?", {playlist_id});
            for (const auto& item : entry.items) {
                raw_execute(db, R"SQL(INSERT INTO playlist_items(playlist_id,position,media_id,source_item_id,source_order,source_path,source_title)
                    VALUES(?,?,?,?,?,?,?))SQL", {playlist_id, item["position"], item["media_id"], item["source_item_id"], item["source_order"],
                                                item["source_path"], item["source_title"]});
            }
        }
        if (source_ids.empty()) raw_execute(db, "DELETE FROM playlists WHERE source='plex'");
        else {
            std::string placeholders;
            json parameters = json::array();
            for (const auto& id : source_ids) { placeholders += (placeholders.empty() ? "" : ",") + std::string("?"); parameters.push_back(id); }
            raw_execute(db, "DELETE FROM playlists WHERE source='plex' AND source_id NOT IN (" + placeholders + ")", parameters);
        }
    });
    return {{"playlists", imported.size()}, {"items", item_total}, {"matched", matched_total}, {"unavailable", item_total - matched_total}};
}

bool is_loopback_address(const std::string& value) {
    in_addr address4{};
    if (::inet_pton(AF_INET, value.c_str(), &address4) == 1) {
        auto host = ntohl(address4.s_addr);
        return (host >> 24) == 127;
    }
    in6_addr address6{};
    return ::inet_pton(AF_INET6, value.c_str(), &address6) == 1 && IN6_IS_ADDR_LOOPBACK(&address6);
}

bool is_local_address(const std::string& value) {
    in_addr address4{};
    if (::inet_pton(AF_INET, value.c_str(), &address4) == 1) {
        auto host = ntohl(address4.s_addr);
        return (host >> 24) == 127 || (host >> 24) == 10 || (host >> 20) == 0xAC1 ||
               (host >> 16) == 0xC0A8 || (host >> 16) == 0xA9FE;
    }
    in6_addr address6{};
    if (::inet_pton(AF_INET6, value.c_str(), &address6) == 1) {
        return IN6_IS_ADDR_LOOPBACK(&address6) || IN6_IS_ADDR_LINKLOCAL(&address6) || (address6.s6_addr[0] & 0xFE) == 0xFC;
    }
    return false;
}

std::string request_header(const httplib::Request& request, const char* key) {
    return request.has_header(key) ? request.get_header_value(key) : std::string{};
}

std::string cookie_value(const httplib::Request& request, std::string_view name) {
    auto cookies = request_header(request, "Cookie");
    std::size_t start = 0;
    while (start < cookies.size()) {
        auto end = cookies.find(';', start);
        if (end == std::string::npos) end = cookies.size();
        auto part = trim(cookies.substr(start, end - start));
        auto equal = part.find('=');
        if (equal != std::string::npos && part.substr(0, equal) == name) return part.substr(equal + 1);
        start = end + 1;
    }
    return {};
}

std::optional<std::string> normalized_origin(std::string value) {
    static const std::regex pattern(R"(^(https?)://([^/:]+)(?::(\d+))?(?:/.*)?$)", std::regex::icase);
    std::smatch match;
    if (!std::regex_match(value, match, pattern)) return std::nullopt;
    auto scheme = lower(match[1]);
    auto host = lower(match[2]);
    int port = match[3].matched ? std::stoi(match[3]) : (scheme == "https" ? 443 : 80);
    return scheme + "://" + host + ":" + std::to_string(port);
}

class FluxaHttpServer {
public:
    explicit FluxaHttpServer(FluxaApp& app) : app_(app) {
        server_.new_task_queue = [] { return new httplib::ThreadPool(24); };
        server_.set_socket_options([](socket_t socket) {
            int enabled = 1;
            ::setsockopt(socket, SOL_SOCKET, SO_REUSEADDR, &enabled, sizeof(enabled));
#ifdef SO_REUSEPORT
            ::setsockopt(socket, SOL_SOCKET, SO_REUSEPORT, &enabled, sizeof(enabled));
#endif
        });
        server_.set_keep_alive_max_count(200);
        server_.set_keep_alive_timeout(15);
        server_.set_read_timeout(30, 0);
        server_.set_write_timeout(3600, 0);
        server_.set_payload_max_length(1024 * 1024);
        server_.set_default_headers({
            {"X-Content-Type-Options", "nosniff"}, {"Referrer-Policy", "same-origin"},
            {"Permissions-Policy", "camera=(), microphone=(), geolocation=()"},
            {"Server", "Fluxa-C++/" + std::string(kVersion)}
        });
        register_routes();
        server_.set_logger([](const httplib::Request& request, const httplib::Response& response) {
            std::cout << request.remote_addr << " - \"" << request.method << ' ' << request.path << "\" " << response.status << '\n';
        });
    }

    bool listen() { return server_.listen(app_.config().host, app_.config().port); }
    void stop() { server_.stop(); }

private:
    FluxaApp& app_;
    httplib::Server server_;
    std::mutex login_mutex_;
    std::unordered_map<std::string, std::vector<std::chrono::steady_clock::time_point>> failed_logins_;

    bool public_proxy(const httplib::Request& request) const {
        return is_loopback_address(request.remote_addr) && request_header(request, "X-Fluxa-Public-Proxy") == "1";
    }

    std::string public_prefix(const httplib::Request& request) const {
        if (!public_proxy(request)) return {};
        auto prefix = request_header(request, "X-Forwarded-Prefix");
        static const std::regex valid(R"(^/[A-Za-z0-9_-]+$)");
        return std::regex_match(prefix, valid) ? prefix : std::string{};
    }

    std::string client_ip(const httplib::Request& request) const {
        if (!public_proxy(request)) return request.remote_addr;
        auto forwarded = request_header(request, "X-Forwarded-For");
        auto comma = forwarded.find(',');
        return trim(forwarded.substr(0, comma));
    }

    bool local_client(const httplib::Request& request) const {
        return !public_proxy(request) && is_local_address(request.remote_addr);
    }

    bool valid_session(const httplib::Request& request) const {
        if (!app_.auth().configured()) return false;
        auto token = cookie_value(request, kCookieName);
        return !token.empty() && app_.auth().verify_session(token);
    }

    bool password_reset_authorized(const httplib::Request& request) const {
        return valid_session(request) || local_client(request) ||
            (public_proxy(request) && is_local_address(client_ip(request)));
    }

    std::string fredplayer_session_token(const httplib::Request& request) const {
        auto token = request_header(request, "X-Fluxa-FredPlayer-Session");
        if (token.empty()) token = cookie_value(request, kFredPlayerCookieName);
        return token;
    }

    bool valid_fredplayer_session(const httplib::Request& request) const {
        auto token = fredplayer_session_token(request);
        return !token.empty() && app_.auth().verify_session(token);
    }

    bool same_origin(const httplib::Request& request, bool require_evidence = false) const {
        auto scheme = public_proxy(request) ? request_header(request, "X-Forwarded-Proto") : "http";
        if (scheme.empty()) scheme = "https";
        auto expected = normalized_origin(scheme + "://" + request_header(request, "Host"));
        auto origin = request_header(request, "Origin");
        if (!origin.empty() && origin != "null") {
            auto candidate = normalized_origin(origin);
            return candidate && expected && *candidate == *expected;
        }
        auto referer = request_header(request, "Referer");
        if (!referer.empty()) {
            auto candidate = normalized_origin(referer);
            return candidate && expected && *candidate == *expected;
        }
        if (lower(request_header(request, "Sec-Fetch-Site")) == "same-origin") return true;
        return !require_evidence;
    }

    bool require_access(const httplib::Request& request, httplib::Response& response) {
        if (local_client(request) || valid_session(request)) return true;
        if (!app_.auth().configured()) {
            send_json(response, {{"error", "Public authentication is not configured"}}, 503);
        } else if (request.path.starts_with("/api/")) {
            send_json(response, {{"error", "Authentication required"}}, 401);
        } else {
            auto destination = public_prefix(request) + request.path;
            auto query = request.target.find('?');
            if (query != std::string::npos) destination += request.target.substr(query);
            response.set_redirect(public_prefix(request) + "/login?next=" + url_encode(destination, true), 303);
        }
        return false;
    }

    template <typename Handler>
    void safe(const httplib::Request& request, httplib::Response& response, bool mutation, Handler&& handler) {
        try {
            if (mutation && !same_origin(request)) throw ApiError(403, "Cross-origin request denied");
            if (!require_access(request, response)) return;
            handler();
        } catch (const ApiError& error) {
            send_json(response, {{"error", error.what()}}, error.status);
        } catch (const json::exception&) {
            send_json(response, {{"error", "Invalid JSON request body"}}, 400);
        } catch (const std::exception& error) {
            send_json(response, {{"error", "Request failed: " + std::string(error.what())}}, 500);
        }
    }

    static json request_json(const httplib::Request& request) {
        if (request.body.empty() || request.body.size() > 1024 * 1024) throw ApiError(400, "A small JSON request body is required");
        auto value = json::parse(request.body);
        if (!value.is_object()) throw ApiError(400, "JSON body must be an object");
        return value;
    }

    static void send_json(httplib::Response& response, const json& payload, int status = 200) {
        response.status = status;
        response.set_header("Cache-Control", "no-store");
        response.set_content(payload.dump(), "application/json; charset=utf-8");
    }

    static void set_cache(httplib::Response& response, const std::string& value) { response.set_header("Cache-Control", value); }

    static void send_file(httplib::Response& response, const fs::path& path, const httplib::Request& request,
                          const std::string& cache = "private, max-age=0, must-revalidate", bool ranges = false) {
        (void)request;
        std::error_code error;
        auto size = fs::file_size(path, error);
        if (error) throw ApiError(404, "File is unavailable");
        if (ranges) response.set_header("Accept-Ranges", "bytes");
        set_cache(response, cache);
        // cpp-httplib applies a validated request Range to this full-length
        // provider and supplies the matching offset to the callback.
        response.set_content_provider(static_cast<std::size_t>(size), mime_type(path),
            [path](std::size_t offset, std::size_t requested, httplib::DataSink& sink) {
                std::ifstream input(path, std::ios::binary);
                if (!input) return false;
                input.seekg(static_cast<std::streamoff>(offset));
                std::vector<char> buffer(std::min<std::size_t>(requested, 1024 * 1024));
                std::size_t remaining = requested;
                while (remaining) {
                    auto count = std::min(remaining, buffer.size());
                    input.read(buffer.data(), static_cast<std::streamsize>(count));
                    auto actual = static_cast<std::size_t>(input.gcount());
                    if (!actual) break;
                    if (!sink.write(buffer.data(), actual)) return false;
                    remaining -= actual;
                }
                return remaining == 0;
            });
    }

    void send_login_page(const httplib::Request& request, httplib::Response& response,
                         const std::string& error = {}, int status = 200, bool force_reset = false) {
        bool reset_mode = force_reset || request.has_param("reset");
        bool reset_authorized = password_reset_authorized(request);
        if (valid_session(request) && error.empty() && !reset_mode) {
            response.set_redirect(public_prefix(request) + "/", 303);
            return;
        }
        auto page = read_file(app_.public_dir() / "login.html");
        auto prefix = public_prefix(request);
        auto next = request.has_param("next") ? request.get_param_value("next") : prefix + "/";
        if (!next.starts_with(prefix + "/")) next = prefix + "/";
        page = replace_all(std::move(page), "{{PREFIX}}", html_escape(prefix));
        page = replace_all(std::move(page), "{{NEXT}}", html_escape(next));
        page = replace_all(std::move(page), "{{ERROR}}", error.empty() ? "" : "<p class=\"error\">" + html_escape(error) + "</p>");
        page = replace_all(std::move(page), "{{RESET_CLASS}}", reset_mode ? "reset-mode" : "");
        page = replace_all(std::move(page), "{{RESET_FORM_CLASS}}", reset_authorized ? "" : "reset-form-unavailable");
        page = replace_all(std::move(page), "{{RESET_INTRO}}", reset_authorized
            ? "Choose a new household password. Devices already signed in will keep their existing 30-day sessions."
            : "For security, open this reset page on a device already signed in to Fluxa, or from your home network.");
        page = replace_all(std::move(page), "{{LOGIN_AUTOFOCUS}}", reset_mode ? "" : "autofocus");
        page = replace_all(std::move(page), "{{RESET_AUTOFOCUS}}", reset_mode && reset_authorized ? "autofocus" : "");
        response.status = status;
        response.set_header("Cache-Control", "no-store");
        std::string frame_ancestors = next.find("tv_shell=1") != std::string::npos ? "file:" : "'none'";
        response.set_header("Content-Security-Policy", std::string("default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; script-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors ") + frame_ancestors);
        response.set_content(page, "text/html; charset=utf-8");
    }

    void handle_login(const httplib::Request& request, httplib::Response& response) {
        try {
            if (!app_.auth().configured()) { send_login_page(request, response, "Public login is not configured on the server.", 503); return; }
            if (!same_origin(request, public_proxy(request))) throw ApiError(403, "Cross-origin login denied");
            auto client = client_ip(request);
            auto now = std::chrono::steady_clock::now();
            {
                std::lock_guard lock(login_mutex_);
                auto& attempts = failed_logins_[client];
                std::erase_if(attempts, [&](auto stamp) { return now - stamp >= 15min; });
                if (attempts.size() >= 10) {
                    response.status = 429; response.set_header("Retry-After", "900"); return;
                }
            }
            auto content_type = request_header(request, "Content-Type");
            if (!content_type.starts_with("application/x-www-form-urlencoded") || request.body.empty() || request.body.size() > 16384) {
                throw ApiError(400, "A form-encoded request is required");
            }
            auto form = parse_form(request.body);
            if (!app_.auth().verify_password(form["password"])) {
                std::lock_guard lock(login_mutex_); failed_logins_[client].push_back(now);
                send_login_page(request, response, "That household password was not accepted.", 401); return;
            }
            { std::lock_guard lock(login_mutex_); failed_logins_.erase(client); }
            auto prefix = public_prefix(request);
            auto next = form.contains("next") ? form["next"] : prefix + "/";
            if (!next.starts_with(prefix + "/")) next = prefix + "/";
            response.set_redirect(next, 303);
            response.set_header("Set-Cookie", std::string(kCookieName) + "=" + app_.auth().issue_session() +
                "; Path=" + (prefix.empty() ? "/" : prefix) + "; Max-Age=" + std::to_string(kSessionSeconds) + "; Secure; HttpOnly; SameSite=Strict");
            response.set_header("Cache-Control", "no-store");
        } catch (const ApiError& error) { send_json(response, {{"error", error.what()}}, error.status); }
        catch (const std::exception& error) { send_json(response, {{"error", error.what()}}, 500); }
    }

    void handle_password_reset(const httplib::Request& request, httplib::Response& response) {
        try {
            if (!same_origin(request, public_proxy(request))) throw ApiError(403, "Cross-origin password reset denied");
            if (!password_reset_authorized(request)) {
                send_login_page(request, response,
                    "Password reset requires a device that is already signed in, or a device on your home network.",
                    403, true);
                return;
            }
            auto content_type = request_header(request, "Content-Type");
            if (!content_type.starts_with("application/x-www-form-urlencoded") ||
                request.body.empty() || request.body.size() > 16384) {
                throw ApiError(400, "A form-encoded request is required");
            }
            auto form = parse_form(request.body);
            auto password = form["password"];
            auto confirmation = form["confirm_password"];
            if (password.size() < 12) throw ApiError(400, "The household password must contain at least 12 characters.");
            if (password.size() > 1024) throw ApiError(400, "The household password is too long.");
            if (password != confirmation) throw ApiError(400, "The new passwords do not match.");
            app_.auth().set_password(password, true);
            response.set_redirect(public_prefix(request) + "/", 303);
            response.set_header("Cache-Control", "no-store");
        } catch (const ApiError& error) {
            send_login_page(request, response, error.what(), error.status, true);
        } catch (const std::exception& error) {
            send_login_page(request, response, "Password reset failed: " + std::string(error.what()), 500, true);
        }
    }

    void register_routes() {
        server_.Options(R"(/.*)", [this](const auto& request, auto& response) {
            safe(request, response, true, [&] {
                response.status = 204;
                response.set_header("Access-Control-Allow-Methods", "GET, HEAD, POST, PUT, OPTIONS");
                response.set_header("Access-Control-Allow-Headers", "Content-Type, Range");
            });
        });
        server_.Get(R"(/(favicon\.svg|favicon\.ico|favicon-16x16\.png|favicon-32x32\.png|apple-touch-icon\.png|android-chrome-192x192\.png|android-chrome-512x512\.png|safari-pinned-tab\.svg|site\.webmanifest))", [this](const auto& request, auto& response) {
            try { send_file(response, app_.public_dir() / request.matches[1].str(), request, "no-cache"); }
            catch (const ApiError& error) { send_json(response, {{"error", error.what()}}, error.status); }
        });
        server_.Get("/login", [this](const auto& request, auto& response) { send_login_page(request, response); });
        server_.Post("/api/auth/login", [this](const auto& request, auto& response) { handle_login(request, response); });
        server_.Post("/api/auth/reset-password", [this](const auto& request, auto& response) { handle_password_reset(request, response); });
        server_.Post("/api/auth/logout", [this](const auto& request, auto& response) {
            safe(request, response, true, [&] {
                auto prefix = public_prefix(request);
                response.set_redirect(prefix + "/login", 303);
                response.set_header("Set-Cookie", std::string(kCookieName) + "=; Path=" + (prefix.empty() ? "/" : prefix) +
                                    "; Max-Age=0; Secure; HttpOnly; SameSite=Strict");
                response.set_header("Cache-Control", "no-store");
            });
        });
        server_.Get("/api/auth/status", [this](const auto& request, auto& response) {
            safe(request, response, false, [&] { send_json(response, {
                {"public_proxy", public_proxy(request)}, {"authentication_required", !local_client(request)},
                {"authenticated", valid_session(request) || local_client(request)}}); });
        });
        server_.Post("/api/fredplayer/authorize", [this](const auto& request, auto& response) {
            safe(request, response, true, [&] {
                auto grant = request_json(request).value("grant", "");
                if (!app_.redeem_fredplayer_grant(grant)) throw ApiError(401, "FredPlayer authorization was not accepted");
                auto prefix = public_prefix(request);
                auto session = app_.auth().issue_session(kRememberedFredPlayerSeconds);
                response.set_header("Set-Cookie", std::string(kFredPlayerCookieName) + "=" +
                    session + "; Path=" +
                    (prefix.empty() ? "/" : prefix) + "; Max-Age=" +
                    std::to_string(kRememberedFredPlayerSeconds) +
                    (public_proxy(request) ? "; Secure" : "") + "; HttpOnly; SameSite=Strict");
                send_json(response, {{"authenticated", true}, {"access_token", session}});
            });
        });
        server_.Get("/api/fredplayer/status", [this](const auto& request, auto& response) {
            safe(request, response, false, [&] {
                send_json(response, {{"authenticated", valid_fredplayer_session(request)}});
            });
        });
        server_.Post("/api/fredplayer/logout", [this](const auto& request, auto& response) {
            safe(request, response, true, [&] {
                auto prefix = public_prefix(request);
                response.set_header("Set-Cookie", std::string(kFredPlayerCookieName) +
                    "=; Path=" + (prefix.empty() ? "/" : prefix) +
                    "; Max-Age=0" + (public_proxy(request) ? "; Secure" : "") +
                    "; HttpOnly; SameSite=Strict");
                send_json(response, {{"authenticated", false}});
            });
        });
        server_.Post("/api/fredplayer/launch", [this](const auto& request, auto& response) {
            safe(request, response, true, [&] {
                if (!valid_fredplayer_session(request)) {
                    throw ApiError(401, "Sign in to FredPlayer first");
                }
                auto body = request_json(request);
                auto ticket = app_.fredplayer_launch_ticket(body);
                send_json(response, {{"ticket", ticket}, {"expires_in", 120}});
            });
        });
        server_.Get("/api/status", [this](const auto& request, auto& response) {
            safe(request, response, false, [&] { send_json(response, app_.status()); });
        });
        server_.Get("/api/libraries", [this](const auto& request, auto& response) {
            safe(request, response, false, [&] { send_json(response, {{"libraries", app_.libraries()}}); });
        });
        server_.Get("/api/playlists", [this](const auto& request, auto& response) {
            safe(request, response, false, [&] { send_json(response, {{"playlists", app_.playlists()}}); });
        });
        server_.Post("/api/playlists", [this](const auto& request, auto& response) {
            safe(request, response, true, [&] { send_json(response, app_.create_playlist(request_json(request)), 201); });
        });
        server_.Post(R"(/api/playlists/(\d+)/items)", [this](const auto& request, auto& response) {
            safe(request, response, true, [&] {
                send_json(response, app_.add_playlist_items(std::stoi(request.matches[1]), request_json(request)));
            });
        });
        server_.Get("/api/fredplayer/library", [this](const auto& request, auto& response) {
            safe(request, response, false, [&] {
                if (!valid_fredplayer_session(request)) {
                    throw ApiError(401, "Sign in to FredPlayer first");
                }
                send_json(response, app_.fredplayer_library(request));
            });
        });
        server_.Get("/api/fredplayer/collections", [this](const auto& request, auto& response) {
            safe(request, response, false, [&] {
                if (!valid_fredplayer_session(request)) {
                    throw ApiError(401, "Sign in to FredPlayer first");
                }
                send_json(response, app_.fredplayer_collections(request));
            });
        });
        server_.Get("/api/fredplayer/artwork", [this](const auto& request, auto& response) {
            safe(request, response, false, [&] {
                if (!valid_fredplayer_session(request) && !local_client(request)) {
                    throw ApiError(401, "Sign in to FredPlayer first");
                }
                auto path = request.has_param("path") ? request.get_param_value("path") : "";
                auto artwork = app_.fredplayer_artwork_path(path);
                if (!artwork) throw ApiError(404, "Artwork not found");
                response.status = 200;
                set_cache(response, "private, max-age=86400");
                response.set_content(std::move(artwork->bytes), artwork->content_type);
            });
        });
        server_.Get(R"(/api/playlists/(\d+))", [this](const auto& request, auto& response) {
            safe(request, response, false, [&] { send_json(response, app_.playlist_detail(std::stoi(request.matches[1]), request)); });
        });
        server_.Get("/api/media", [this](const auto& request, auto& response) {
            safe(request, response, false, [&] { send_json(response, app_.media_list(request)); });
        });
        server_.Get("/api/compressor-settings", [this](const auto& request, auto& response) {
            safe(request, response, false, [&] { send_json(response, app_.compressor_settings()); });
        });
        server_.Put("/api/compressor-settings", [this](const auto& request, auto& response) {
            safe(request, response, true, [&] { send_json(response, app_.update_global_compressor(request_json(request))); });
        });
        server_.Get(R"(/api/media/(\d+))", [this](const auto& request, auto& response) {
            safe(request, response, false, [&] { send_json(response, app_.media_detail(std::stoi(request.matches[1]))); });
        });
        server_.Get(R"(/api/media/(\d+)/compressor-settings)", [this](const auto& request, auto& response) {
            safe(request, response, false, [&] { send_json(response, app_.compressor_settings(std::stoi(request.matches[1]))); });
        });
        server_.Put(R"(/api/media/(\d+)/compressor-settings)", [this](const auto& request, auto& response) {
            safe(request, response, true, [&] { send_json(response, app_.update_media_compressor(std::stoi(request.matches[1]), request_json(request))); });
        });
        server_.Put(R"(/api/media/(\d+)/progress)", [this](const auto& request, auto& response) {
            safe(request, response, true, [&] { send_json(response, app_.update_progress(std::stoi(request.matches[1]), request_json(request))); });
        });
        server_.Post("/api/scan", [this](const auto& request, auto& response) {
            safe(request, response, true, [&] {
                auto results = app_.scan();
                if (app_.config().probe_on_start) app_.start_probe_worker();
                app_.start_artwork_backfill();
                send_json(response, {{"results", results}});
            });
        });
        server_.Post(R"(/api/media/(\d+)/probe)", [this](const auto& request, auto& response) {
            safe(request, response, true, [&] { send_json(response, app_.probe_media(std::stoi(request.matches[1]))); });
        });
        server_.Post(R"(/api/media/(\d+)/analyze)", [this](const auto& request, auto& response) {
            safe(request, response, true, [&] {
                int id = std::stoi(request.matches[1]);
                bool started = app_.start_analysis(id);
                send_json(response, {{"media_id", id}, {"status", "running"}, {"started", started}}, 202);
            });
        });
        server_.Get(R"(/api/media/(\d+)/loudness)", [this](const auto& request, auto& response) {
            safe(request, response, false, [&] { send_json(response, app_.media_detail(std::stoi(request.matches[1]), false)["analysis"]); });
        });
        server_.Post(R"(/api/media/(\d+)/compatibility)", [this](const auto& request, auto& response) {
            safe(request, response, true, [&] { send_json(response, app_.start_compatibility(std::stoi(request.matches[1]), request_json(request)), 201); });
        });
        server_.Post(R"(/api/compat/([A-Za-z0-9_-]{20,40})/control)", [this](const auto& request, auto& response) {
            safe(request, response, true, [&] { send_json(response, app_.control_compatibility(request.matches[1], request_json(request))); });
        });
        server_.Get(R"(/api/compat/([A-Za-z0-9_-]{20,40})/(index\.m3u8|init\.mp4|segment-\d{6}\.(?:ts|m4s)))",
            [this](const auto& request, auto& response) {
                safe(request, response, false, [&] {
                    auto name = request.matches[2].str();
                    send_file(response, app_.compatibility_file(request.matches[1], name), request,
                              name.ends_with(".m3u8") ? "no-store" : "private, max-age=3600");
                });
            });
        server_.Get(R"(/api/media/(\d+)/stream)", [this](const auto& request, auto& response) {
            safe(request, response, false, [&] { send_file(response, app_.resolve_media_path(std::stoi(request.matches[1])), request,
                "private, max-age=0, must-revalidate", true); });
        });
        server_.Get(R"(/api/media/(\d+)/thumbnail)", [this](const auto& request, auto& response) {
            safe(request, response, false, [&] {
                auto media_id = std::stoi(request.matches[1]);
                if (auto artwork = app_.fredplayer_artwork(media_id)) {
                    response.status = 200;
                    set_cache(response, "private, max-age=86400");
                    response.set_content(std::move(artwork->bytes), artwork->content_type);
                    return;
                }
                auto path = app_.thumbnail(media_id);
                if (path.empty()) { response.status = 202; response.set_header("Retry-After", "2"); response.set_header("Cache-Control", "no-store"); }
                else send_file(response, path, request, "private, max-age=86400");
            });
        });
        server_.Get(R"(/api/media/(\d+)/chapters/(\d+)/thumbnail)", [this](const auto& request, auto& response) {
            safe(request, response, false, [&] {
                auto path = app_.chapter_thumbnail(std::stoi(request.matches[1]), std::stoi(request.matches[2]));
                if (path.empty()) { response.status = 202; response.set_header("Retry-After", "2"); response.set_header("Cache-Control", "no-store"); }
                else send_file(response, path, request, "private, max-age=86400");
            });
        });
        server_.Get(R"(/api/media/(\d+)/subtitles/(\d+)\.vtt)", [this](const auto& request, auto& response) {
            safe(request, response, false, [&] { send_file(response, app_.caption(std::stoi(request.matches[1]), std::stoi(request.matches[2])),
                request, "private, max-age=86400"); });
        });
        server_.Post("/api/playback-events", [this](const auto& request, auto& response) {
            safe(request, response, true, [&] { send_json(response, app_.record_playback_event(request_json(request), request_header(request, "User-Agent")), 202); });
        });
        server_.Get(R"(/.*)", [this](const auto& request, auto& response) {
            safe(request, response, false, [&] {
                static const std::unordered_map<std::string, std::string> files = {
                    {"/", "index.html"}, {"/index.html", "index.html"}, {"/style.css", "style.css"},
                    {"/script.js", "script.js"}, {"/tv-input.js", "tv-input.js"}, {"/vendor/hls.min.js", "vendor/hls.min.js"}
                };
                auto found = files.find(url_decode(request.path, false));
                if (found == files.end()) throw ApiError(404, "Page not found");
                std::string frame_ancestors = request.has_param("tv_shell") && request.get_param_value("tv_shell") == "1"
                    ? "file:" : "'self'";
                response.set_header("Content-Security-Policy", std::string("default-src 'self'; frame-src https://patrick-lamphier.com; media-src 'self' blob:; connect-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; img-src 'self' data:; frame-ancestors ") + frame_ancestors);
                send_file(response, app_.public_dir() / found->second, request, "no-cache");
            });
        });
        server_.set_error_handler([](const auto&, auto& response) {
            if (response.status == 404 && response.body.empty()) send_json(response, {{"error", "Endpoint not found"}}, 404);
        });
    }
};

} // namespace

namespace {
FluxaHttpServer* g_server = nullptr;

void stop_server(int) {
    if (g_server) g_server->stop();
}

std::string private_input(const char* prompt) {
    std::cerr << prompt << std::flush;
    termios previous{};
    bool terminal = ::isatty(STDIN_FILENO) && ::tcgetattr(STDIN_FILENO, &previous) == 0;
    if (terminal) {
        auto hidden = previous;
        hidden.c_lflag &= static_cast<tcflag_t>(~ECHO);
        ::tcsetattr(STDIN_FILENO, TCSAFLUSH, &hidden);
    }
    std::string value;
    std::getline(std::cin, value);
    if (terminal) {
        ::tcsetattr(STDIN_FILENO, TCSAFLUSH, &previous);
        std::cerr << '\n';
    }
    return value;
}

void usage() {
    std::cout << "Usage: fluxa-server [--config PATH] [serve|status|scan|probe ID|analyze ID|auth-init|auth-set-password|auth-status|import-plex-playlists [--plex-db PATH]]\n";
}
} // namespace

int main(int argc, char** argv) {
    try {
        fs::path config_path = "fluxa.json";
        std::string command = "serve";
        std::vector<std::string> arguments;
        for (int i = 1; i < argc; ++i) {
            std::string value = argv[i];
            if (value == "--config") {
                if (++i >= argc) throw std::runtime_error("--config requires a path");
                config_path = argv[i];
            } else if (value == "-h" || value == "--help") {
                usage(); return 0;
            } else if (command == "serve" && arguments.empty() &&
                       (value == "serve" || value == "status" || value == "scan" || value == "probe" || value == "analyze" ||
                        value == "auth-init" || value == "auth-set-password" || value == "auth-status" || value == "import-plex-playlists")) {
                command = value;
            } else arguments.push_back(value);
        }
        FluxaApp app(load_config(config_path));
        if (command == "status") {
            std::cout << app.status().dump(2) << '\n'; return 0;
        }
        if (command == "scan") {
            auto results = app.scan();
            for (const auto& result : results) {
                std::cout << result.value("library", "") << ": " << result.value("discovered", 0) << " found, "
                          << result.value("added", 0) << " added, " << result.value("changed", 0) << " changed, "
                          << result.value("renamed", 0) << " renamed, "
                          << result.value("unavailable", 0) << " unavailable, " << result.value("skipped", 0) << " skipped";
                if (!result["error"].is_null()) std::cout << " (error=" << result["error"].get<std::string>() << ')';
                std::cout << '\n';
            }
            return 0;
        }
        if (command == "probe" || command == "analyze") {
            if (arguments.empty()) throw std::runtime_error(command + " requires a media ID");
            int id = std::stoi(arguments.front());
            if (command == "probe") std::cout << app.probe_media(id).dump(2) << '\n';
            else { app.analyze_now(id); std::cout << "Analyzed media " << id << ".\n"; }
            return 0;
        }
        if (command == "auth-init") {
            std::cout << "Public authentication initialized. Temporary password: " << app.auth().initialize() << '\n';
            return 0;
        }
        if (command == "auth-set-password") {
            auto password = private_input("New household password: ");
            auto confirmation = private_input("Confirm household password: ");
            if (password != confirmation) { std::cerr << "Passwords do not match\n"; return 1; }
            app.auth().set_password(password);
            std::cout << "Household password changed; existing public sessions are no longer valid.\n";
            return 0;
        }
        if (command == "auth-status") {
            std::cout << json({{"configured", app.auth().configured()}, {"auth_file", app.auth().path().string()},
                              {"temporary_password_file", fs::is_regular_file(app.config().data_dir / "initial-password.txt")
                                  ? json((app.config().data_dir / "initial-password.txt").string()) : json(nullptr)}}).dump(2) << '\n';
            return 0;
        }
        if (command == "import-plex-playlists") {
            fs::path plex = "/var/lib/plexmediaserver/Library/Application Support/Plex Media Server/Plug-in Support/Databases/com.plexapp.plugins.library.db";
            for (std::size_t i = 0; i < arguments.size(); ++i) {
                if (arguments[i] == "--plex-db" && i + 1 < arguments.size()) plex = arguments[++i];
            }
            auto result = app.import_plex_playlists(plex);
            std::cout << "Imported " << result["playlists"] << " Plex playlists with " << result["items"] << " items: "
                      << result["matched"] << " matched, " << result["unavailable"] << " unavailable.\n";
            return 0;
        }
        if (command != "serve") { usage(); return 2; }

        std::jthread startup;
        if (app.config().scan_on_start) {
            startup = std::jthread([&app] {
                try { app.scan(); if (app.config().probe_on_start) app.start_probe_worker(); }
                catch (const std::exception& error) { std::cerr << "Startup scan failed: " << error.what() << '\n'; }
            });
        } else if (app.config().probe_on_start) app.start_probe_worker();
        app.start_artwork_backfill();
        FluxaHttpServer server(app);
        g_server = &server;
        std::signal(SIGINT, stop_server);
        std::signal(SIGTERM, stop_server);
        std::cout << "Fluxa " << kVersion << " (C++20) listening on http://" << app.config().host << ':' << app.config().port << '\n';
        bool listened = server.listen();
        g_server = nullptr;
        return listened ? 0 : 1;
    } catch (const std::exception& error) {
        std::cerr << "Fluxa error: " << error.what() << '\n';
        return 1;
    }
}
