// SPDX-License-Identifier: GPL-3.0-only
// Bounded validation workload, not a benchmark. See tools/README.md.
#include <musa_runtime.h>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <csignal>
#include <cstddef>
#include <exception>
#include <iostream>
#include <mutex>
#include <sstream>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>
#include <unistd.h>

namespace {
// A lock-free atomic is both signal-safe and visible to every worker thread.
static_assert(ATOMIC_INT_LOCK_FREE == 2, "signal handler requires lock-free atomic int");
std::atomic<int> received_signal{0};
void on_signal(int signal) { received_signal.store(signal, std::memory_order_relaxed); }

std::string quoted(const std::string& text) {
    std::ostringstream out;
    out << '"';
    for (unsigned char ch : text) {
        if (ch == '"' || ch == '\\') out << '\\' << ch;
        else if (ch >= 32 && ch < 127) out << ch;
        else {
            const char* hex = "0123456789abcdef";
            out << "\\u00" << hex[ch >> 4] << hex[ch & 15];
        }
    }
    out << '"';
    return out.str();
}

int integer(const std::string& text, int minimum, int maximum) {
    if (text.empty() || text.find_first_not_of("0123456789") != std::string::npos)
        throw std::runtime_error("expected an unsigned integer: " + text);
    std::size_t end = 0;
    const long value = std::stol(text, &end);
    if (end != text.size() || value < minimum || value > maximum)
        throw std::runtime_error("integer outside allowed range: " + text);
    return static_cast<int>(value);
}

std::vector<int> device_list(const std::string& text) {
    std::vector<int> devices;
    std::istringstream input(text);
    std::string field;
    if (text.empty() || text.back() == ',') throw std::runtime_error("empty device id");
    while (std::getline(input, field, ',')) {
        const int id = integer(field, 0, 65535);
        if (std::find(devices.begin(), devices.end(), id) != devices.end())
            throw std::runtime_error("duplicate device id");
        devices.push_back(id);
    }
    return devices;
}

void check(musaError_t result, const char* operation) {
    if (result != musaSuccess)
        throw std::runtime_error(std::string(operation) + ": " + musaGetErrorString(result));
}

__global__ void update(float* values, std::size_t count) {
    const std::size_t stride = static_cast<std::size_t>(blockDim.x) * gridDim.x;
    for (std::size_t i = static_cast<std::size_t>(blockIdx.x) * blockDim.x + threadIdx.x;
         i < count; i += stride) {
        float value = values[i];
        for (int j = 0; j < 16; ++j) value = value * 0.99999f + 0.00001f;
        values[i] = value;
    }
}
}  // namespace

int main(int argc, char** argv) {
    try {
        std::vector<int> devices{0};
        int seconds = 20, mib = 64;
        bool info = false;
        for (int i = 1; i < argc; ++i) {
            const std::string option = argv[i];
            if (option == "--help" || option == "-h") {
                std::cout << "gpu_smoke [--devices 0,1,...] [--seconds 1..60] [--mib 1..256]\n"
                             "gpu_smoke --info  (query all device properties; no workload)\n";
                return 0;
            }
            if (option == "--info") { info = true; continue; }
            if (option != "--devices" && option != "--seconds" && option != "--mib")
                throw std::runtime_error("unknown option: " + option);
            if (++i == argc) throw std::runtime_error("missing value for " + option);
            if (option == "--devices") devices = device_list(argv[i]);
            else if (option == "--seconds") seconds = integer(argv[i], 1, 60);
            else mib = integer(argv[i], 1, 256);
        }

        int count = 0;
        check(musaGetDeviceCount(&count), "musaGetDeviceCount");
        if (info) {
            for (int id = 0; id < count; ++id) {
                musaDeviceProp properties{};
                check(musaGetDeviceProperties(&properties, id), "musaGetDeviceProperties");
                std::cout << "{\"event\":\"device\",\"device\":" << id
                          << ",\"name\":" << quoted(properties.name)
                          << ",\"major\":" << properties.major << ",\"minor\":" << properties.minor
                          << ",\"warp_size\":" << properties.warpSize
                          << ",\"total_memory_bytes\":" << properties.totalGlobalMem << "}\n";
            }
            return 0;
        }
        for (int id : devices)
            if (id >= count) throw std::runtime_error("device not found: " + std::to_string(id));

        std::signal(SIGINT, on_signal);
        std::signal(SIGTERM, on_signal);
        const std::size_t bytes = static_cast<std::size_t>(mib) * 1024 * 1024;
        const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(seconds);
        std::atomic<bool> failed{false};
        std::atomic<std::size_t> ready{0};
        std::mutex error_mutex;
        auto fail = [&](int id, const std::string& message) {
            failed.store(true);
            std::lock_guard<std::mutex> lock(error_mutex);
            std::cerr << "{\"event\":\"error\",\"device\":" << id
                      << ",\"message\":" << quoted(message) << "}" << std::endl;
        };
        auto stopping = [&]() {
            return failed.load() || received_signal.load() != 0 || std::chrono::steady_clock::now() >= deadline;
        };
        std::vector<std::thread> workers;
        try {
            for (int id : devices) {
                workers.emplace_back([&, id]() {
                    float* data = nullptr;
                    try {
                        if (stopping()) return;
                        check(musaSetDevice(id), "musaSetDevice");
                        check(musaMalloc(reinterpret_cast<void**>(&data), bytes), "musaMalloc");
                        check(musaMemset(data, 0, bytes), "musaMemset");
                        check(musaDeviceSynchronize(), "initial musaDeviceSynchronize");
                        // A fixed block size needs no assumption about hardware warp size.
                        update<<<512, 128>>>(data, bytes / sizeof(float));
                        check(musaGetLastError(), "initial kernel launch");
                        check(musaDeviceSynchronize(), "initial kernel synchronize");
                        ready.fetch_add(1);
                        while (!stopping()) {
                            const auto active_until = std::chrono::steady_clock::now()
                                                      + std::chrono::milliseconds(20);
                            do {
                                update<<<512, 128>>>(data, bytes / sizeof(float));
                                check(musaGetLastError(), "kernel launch");
                                check(musaDeviceSynchronize(), "kernel synchronize");
                            } while (!stopping() && std::chrono::steady_clock::now() < active_until);
                            std::this_thread::sleep_for(std::chrono::milliseconds(80));
                        }
                    } catch (const std::exception& error) { fail(id, error.what()); }
                    if (data != nullptr) {
                        const auto result = musaFree(data);
                        if (result != musaSuccess) fail(id, std::string("musaFree: ") + musaGetErrorString(result));
                    }
                });
            }
        } catch (const std::exception& error) { fail(-1, error.what()); }

        while (ready.load() != devices.size() && !stopping())
            std::this_thread::sleep_for(std::chrono::milliseconds(10));
        if (ready.load() == devices.size() && !failed.load()) {
            std::cout << "{\"event\":\"started\",\"pid\":" << getpid() << ",\"devices\":[";
            for (std::size_t i = 0; i < devices.size(); ++i) {
                if (i) std::cout << ',';
                std::cout << devices[i];
            }
            std::cout << "],\"allocated_bytes_per_device\":" << bytes << ",\"seconds\":" << seconds << "}" << std::endl;
        }
        for (auto& worker : workers) worker.join();
        if (ready.load() != devices.size() && !received_signal.load() && !failed.load())
            fail(-1, "deadline expired before all devices were ready");
        const int signal = received_signal.load();
        const int exit_code = failed.load() ? 1 : (signal ? 128 + signal : 0);
        std::cout << "{\"event\":\"completed\",\"pid\":" << getpid()
                  << ",\"signal\":" << signal << ",\"exit_code\":" << exit_code << "}" << std::endl;
        return exit_code;
    } catch (const std::exception& error) {
        std::cerr << "{\"event\":\"error\",\"message\":" << quoted(error.what()) << "}" << std::endl;
        return 1;
    }
}
