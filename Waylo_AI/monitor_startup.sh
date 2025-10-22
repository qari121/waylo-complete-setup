#!/bin/bash
# Monitor Wailo Complete Startup Process
# Shows real-time status of all startup steps

echo "🔍 Wailo Startup Process Monitor"
echo "================================"
echo ""

# Check if running as root
if [ "$EUID" -ne 0 ]; then
    echo "❌ Please run as root (sudo) for full monitoring"
    echo "   Some features may not work without sudo"
fi

echo "📋 Startup Flow: WiFi → Bluetooth → MAC exchange → Bluetooth off → AI_Chat"
echo ""

# Function to check service status
check_service() {
    local service=$1
    local status=$(systemctl is-active $service 2>/dev/null || echo "not-found")
    case $status in
        "active") echo "✅ $service: Running" ;;
        "inactive") echo "⏸️ $service: Stopped" ;;
        "activating") echo "🔄 $service: Starting..." ;;
        "deactivating") echo "🔄 $service: Stopping..." ;;
        "failed") echo "❌ $service: Failed" ;;
        "not-found") echo "❓ $service: Not found" ;;
        *) echo "❓ $service: $status" ;;
    esac
}

# Function to check progress files
check_progress() {
    echo ""
    echo "📁 Progress Files:"
    
    if [ -f /home/orangepi/Waylo_AI/.startup_complete ]; then
        echo "✅ Startup sequence: COMPLETE"
    else
        echo "⏳ Startup sequence: In progress..."
    fi
    
    if [ -f /home/orangepi/Waylo_AI/.bluetooth_handoff_complete ]; then
        echo "✅ BLE handoff: COMPLETE"
    else
        echo "⏳ BLE handoff: Waiting for iOS connection..."
    fi
}

# Function to check network status
check_network() {
    echo ""
    echo "🌐 Network Status:"
    
    if systemctl is-active --quiet network-online.target; then
        echo "✅ Network: Online"
    else
        echo "⏳ Network: Waiting for connectivity..."
    fi
    
    # Check WiFi/AP status
    if iw dev wlan0 info 2>/dev/null | grep -q "type AP"; then
        echo "📡 WiFi: AP Mode Active"
    elif ip addr show wlan0 2>/dev/null | grep -q "inet "; then
        echo "📡 WiFi: Client Mode (Connected)"
    else
        echo "📡 WiFi: Not connected"
    fi
}

# Function to check Bluetooth status
check_bluetooth() {
    echo ""
    echo "🔵 Bluetooth Status:"
    
    if systemctl is-active --quiet bluetooth.service; then
        echo "✅ Bluetooth service: Running"
        
        # Check if device is discoverable
        if bluetoothctl show 2>/dev/null | grep -q "Discoverable: yes"; then
            echo "📱 Device: Discoverable"
        else
            echo "📱 Device: Not discoverable"
        fi
        
        # Check active instances
        instances=$(bluetoothctl show 2>/dev/null | grep "ActiveInstances" | awk '{print $2}')
        if [ "$instances" = "0x00" ]; then
            echo "📱 Advertising: None"
        else
            echo "📱 Advertising: Active ($instances)"
        fi
    else
        echo "❌ Bluetooth service: Not running"
    fi
}

# Function to check AI_Chat status
check_aichat() {
    echo ""
    echo "🤖 AI_Chat Status:"
    
    if systemctl is-active --quiet wailo.service; then
        echo "✅ AI_Chat service: Running"
        
        # Check if process is actually running
        if pgrep -f "AI_Chat.py" > /dev/null; then
            echo "🧠 AI_Chat process: Active"
        else
            echo "🧠 AI_Chat process: Not found"
        fi
    else
        echo "⏸️ AI_Chat service: Not running"
    fi
}

# Main monitoring loop
echo "🔄 Starting continuous monitoring (Press Ctrl+C to stop)..."
echo ""

while true; do
    # Clear screen for better readability
    clear
    
    echo "🔍 Wailo Startup Process Monitor - $(date)"
    echo "================================================"
    echo ""
    
    # Check all components
    check_network
    check_service "wailo-complete-startup.service"
    check_service "wailo-ble-handoff.service"
    check_service "wailo.service"
    check_bluetooth
    check_aichat
    check_progress
    
    echo ""
    echo "🔄 Refreshing in 5 seconds... (Press Ctrl+C to stop)"
    echo "💡 Tips:"
    echo "   - Look for '✅' to see completed steps"
    echo "   - Look for '⏳' to see steps in progress"
    echo "   - Look for '❌' to see any failures"
    echo ""
    
    sleep 5
done
