// SPDX-License-Identifier: Apache-2.0
// Export the hash-pinned core's event dispatcher and Bluetooth ownership helpers.
// Analysis overrides are rolled back; vendor decompilation is a private output.
import ghidra.app.script.GhidraScript;
import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileResults;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.Function;
import ghidra.program.model.listing.ParameterImpl;
import ghidra.program.model.data.PointerDataType;
import ghidra.program.model.data.VoidDataType;
import ghidra.program.model.pcode.JumpTable;
import ghidra.program.model.symbol.SourceType;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
import java.nio.file.attribute.PosixFilePermissions;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.HexFormat;
import java.util.Set;

public class ExportBluetoothPolicy extends GhidraScript {
    private static final String CORE =
        "dfc83b247b505aa08ea62060f999192112a47b606754d5f469357b7addf0295a";
    private static final String TABLE =
        "efe7930c2845f120edc0f79c70246beaf713ed1b1a359ae37ab57fb1107769e4";

    public void run() throws Exception {
        String[] args = getScriptArgs();
        if (args.length != 1 || !CORE.equals(currentProgram.getExecutableSHA256()))
            throw new IllegalArgumentException("Expected private output path and reviewed core");
        Address entry = toAddr(0x64c8c);
        Address branch = toAddr(0x64dbc);
        Address table = toAddr(0x64dc4);
        Function function = getFunctionAt(entry);
        if (function == null || !function.getBody().contains(branch))
            throw new IllegalStateException("Missing event function or switch instruction");
        byte[] bytes = new byte[48 * 4];
        if (currentProgram.getMemory().getBytes(table, bytes) != bytes.length ||
            !TABLE.equals(HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(bytes))))
            throw new IllegalStateException("Unreviewed switch table");
        ArrayList<Address> targets = new ArrayList<>();
        for (int i = 0; i < 48; i++) {
            Address target = toAddr(Integer.toUnsignedLong(getInt(table.add(i * 4))));
            if (!function.getBody().contains(target) || getInstructionAt(target) == null)
                throw new IllegalStateException("Switch target outside analyzed event function");
            targets.add(target);
        }
        Path output = Path.of(args[0]);
        // CREATE_NEW refuses an existing file or symlink. Parent privacy is the caller's responsibility.
        Files.createFile(output, PosixFilePermissions.asFileAttribute(
            PosixFilePermissions.fromString("rw-------")));
        int transaction = currentProgram.startTransaction("Temporary event switch override");
        DecompInterface decompiler = new DecompInterface();
        try {
            new JumpTable(branch, targets, true, 0).writeOverride(function);
            // The auto-analyzed import had one parameter. That incorrectly hid
            // the Connected=true branch by treating its stack output as constant.
            Function basic = getFunctionAt(toAddr(0x16608));
            if (basic == null || !basic.getName().equals("dbus_message_iter_get_basic"))
                throw new IllegalStateException("Unexpected D-Bus import");
            if (basic.isThunk()) basic = basic.getThunkedFunction(true);
            basic.replaceParameters(Function.FunctionUpdateType.DYNAMIC_STORAGE_ALL_PARAMS,
                true, SourceType.USER_DEFINED,
                new ParameterImpl("iter", new PointerDataType(VoidDataType.dataType), currentProgram),
                new ParameterImpl("value", new PointerDataType(VoidDataType.dataType), currentProgram));
            basic.setReturnType(VoidDataType.dataType, SourceType.USER_DEFINED);
            if (!decompiler.openProgram(currentProgram))
                throw new IllegalStateException("Cannot open decompiler");
            DecompileResults result = decompiler.decompileFunction(function, 60, monitor);
            if (!result.decompileCompleted())
                throw new IllegalStateException(result.getErrorMessage());
            Set<Address> recovered = new HashSet<>();
            for (JumpTable jump : result.getHighFunction().getJumpTables()) {
                if (branch.equals(jump.getSwitchAddress()))
                    for (Address address : jump.getCases()) recovered.add(address);
            }
            if (!recovered.equals(new HashSet<>(targets)))
                throw new IllegalStateException("Decompiler did not recover the complete target set");
            StringBuilder text = new StringBuilder(
                "// Private analysis output, not original source or a redistribution grant.\n"
                + "// Original core SHA-256: " + CORE + "\n"
                + "// Verified switch table: 48 entries, " + recovered.size() + " distinct targets.\n"
                + "// Decompiler case labels are destination addresses; event IDs map below.\n");
            for (int i = 0; i < targets.size(); i++)
                text.append("// Event ").append(i + 1).append(" -> ").append(targets.get(i)).append('\n');
            text.append(result.getDecompiledFunction().getC());
            for (long address : new long[] {0x38318, 0x38538, 0x38828, 0x36cb8, 0x36d70,
                                           0x86f18, 0x870cc}) {
                Function helper = getFunctionAt(toAddr(address));
                if (helper == null) throw new IllegalStateException("Missing Bluetooth helper");
                result = decompiler.decompileFunction(helper, 60, monitor);
                if (!result.decompileCompleted())
                    throw new IllegalStateException(result.getErrorMessage());
                text.append("\n// FUNCTION ").append(helper.getEntryPoint()).append('\n')
                    .append(result.getDecompiledFunction().getC());
            }
            Files.writeString(output, text, StandardCharsets.UTF_8, StandardOpenOption.WRITE);
            println("Recovered 48 event-table entries and " + recovered.size()
                + " distinct targets; temporary analysis override will be rolled back.");
        } finally {
            decompiler.dispose();
            currentProgram.endTransaction(transaction, false);
        }
    }
}
