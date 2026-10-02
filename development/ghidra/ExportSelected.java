// SPDX-License-Identifier: Apache-2.0
// Export selected decompilations into a private file, outside this repository.
// Arguments: output-file function-name-regex
import ghidra.app.script.GhidraScript;
import ghidra.app.decompiler.*;
import ghidra.program.model.listing.*;
import java.io.*;
import java.util.regex.Pattern;

public class ExportSelected extends GhidraScript {
    public void run() throws Exception {
        String[] args = getScriptArgs();
        if (args.length != 2) throw new IllegalArgumentException("output-file function-name-regex");
        Pattern pattern = Pattern.compile(args[1]);
        DecompInterface decompiler = new DecompInterface();
        decompiler.openProgram(currentProgram);
        try (PrintWriter out = new PrintWriter(new FileOutputStream(args[0], false))) {
            FunctionIterator functions = currentProgram.getFunctionManager().getFunctions(true);
            while (functions.hasNext() && !monitor.isCancelled()) {
                Function function = functions.next();
                if (!pattern.matcher(function.getName()).matches()) continue;
                out.println("\n// FUNCTION " + function.getName() + " " + function.getEntryPoint());
                DecompileResults result = decompiler.decompileFunction(function, 60, monitor);
                if (result.decompileCompleted()) out.println(result.getDecompiledFunction().getC());
                else out.println("// ERROR " + result.getErrorMessage());
                out.flush();
            }
        } finally {
            decompiler.dispose();
        }
    }
}
